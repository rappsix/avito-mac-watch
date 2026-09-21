"""
index.py — функция для Yandex Cloud Functions: следит за Авито и пишет в Телеграм.

Диска у функции нет, поэтому список уже показанных объявлений хранится
в закреплённом сообщении самого телеграм-чата — никаких внешних хранилищ.

Переменные окружения функции:
    TG_TOKEN    токен бота от @BotFather          (обязательно)
    TG_CHAT     chat_id получателя                (обязательно)
    SEARCHES    JSON со списком поисков           (необязательно)
    FILTERS     JSON с фильтрами                  (необязательно)
"""

import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape as html_unescape


class BlockedError(RuntimeError):
    """Авито отдал капчу или заглушку вместо выдачи."""


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

STATE_MARKER = "avito-watch-state"
STATE_KEEP = 300

DEFAULT_SEARCHES = [
    {"label": "M2 Pro 32GB",
     "url": "https://www.avito.ru/moskva/noutbuki?q=macbook+pro+m2+pro+32&s=104"},
    {"label": "Pro 32GB общий",
     "url": "https://www.avito.ru/moskva/noutbuki?q=macbook+pro+32gb&s=104"},
]

DEFAULT_FILTERS = {
    "price_min": 90000,
    "price_max": 145000,
    "require_ram_gb": 32,
    "title_must_match": "macbook|макбук",
    "chip_must_match": r"\bm[1-5]\b",
    "blacklist": [
        "полос", "битый пиксел", "замена экрана", "заменен экран", "замене экран",
        "разбит", "трещин", "не работает экран", "нет изображен",
        "mdm", "мдм", "только обмен", "обмен на", "оптом", "на запчаст",
        "под восстановление", "не включается", "залит", "после залит",
        "intel", r"\bi[579]\b", "radeon", "touch bar", "тачбар",
    ],
    "max_cycles": 600,
    "min_battery_pct": 85,
}


def log(msg):
    print(msg, flush=True)


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------- разбор страницы

ITEM_SPLIT = re.compile(r'(?=<div[^>]*data-marker="item"[\s>])')
ITEM_ID = re.compile(r'data-item-id="(\d+)"')
ITEM_LINK = re.compile(r'<a[^>]+href="(/[^"#]*?_\d{6,})(?:\?[^"#]*)?"[^>]*>(.*?)</a>', re.S)
PRICE_TAG = re.compile(r">\s*(\d[\d\u00a0\u202f ]*)\s*₽\s*<")
SCRIPTS = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
TAGS = re.compile(r"<[^>]+>")
CAPTCHA_HINTS = ("доступ ограничен", "подтвердите, что вы не робот",
                 "firewall", "ваши действия похожи на автоматические")


def strip_tags(fragment):
    text = SCRIPTS.sub(" ", fragment)
    text = TAGS.sub(" ", text)
    text = html_unescape(text)
    text = text.replace(" ", " ").replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


def extract_items(page):
    """
    Разбирает выдачу Авито. Страница приходит уже отрендеренной на сервере:
    никакого JSON в разметке нет, поэтому идём по блокам data-marker="item".
    """
    if any(hint in page.lower() for hint in CAPTCHA_HINTS):
        raise BlockedError("Авито показал заглушку вместо выдачи")

    items = []
    for chunk in ITEM_SPLIT.split(page):
        m_id = ITEM_ID.search(chunk)
        if not m_id:
            continue

        # ссылка и заголовок: берём якорь с самым длинным текстом,
        # потому что тот же href висит ещё и на картинке-превью
        best_text, best_href = "", None
        for m in ITEM_LINK.finditer(chunk):
            txt = strip_tags(m.group(2))
            if len(txt) > len(best_text):
                best_text, best_href = txt, m.group(1)
        if not best_href:
            continue

        # цена живёт в собственном теге — так к ней не прилипают цифры из заголовка
        m_price = PRICE_TAG.search(html_unescape(chunk))
        if not m_price:
            continue
        price = int(re.sub(r"\D", "", m_price.group(1)))
        text = strip_tags(chunk)
        if price < 1000:
            continue

        items.append({
            "id": m_id.group(1),
            "title": best_text[:120],
            "price": price,
            "url": "https://www.avito.ru" + best_href,
            "text": text[:1200],
        })

    uniq = {}
    for it in items:
        uniq.setdefault(it["id"], it)
    return list(uniq.values())


# ---------------------------------------------------------------- фильтры

def ram_gb(text):
    """
    Объём памяти: '32gb', '32 гб', '32/512', '32/1024 gb', '32/1tb'.
    Сначала пробуем запись через слэш — иначе из '1024 gb' выкусывается '24'.
    """
    known = (8, 16, 18, 24, 32, 36, 48, 64, 96)
    patterns = (
        r"\b(\d{2})\s*/\s*(?:\d{3,4}|\d\s*(?:tb|тб))",   # 32/512, 32/1024, 32/1tb
        r"\b(\d{2})\s*(?:gb|гб)\b",                       # 32gb, 32 ГБ
        r"\b(\d{2})\s*(?:gb|гб)",                          # 32gb без границы справа
    )
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            val = int(m.group(1))
            if val in known:
                return val
    return None


def cycles(text):
    m = re.search(r"(\d{1,4})\s*цикл", text, re.I)
    return int(m.group(1)) if m else None


def battery_pct(text):
    m = re.search(r"(?:акб|аккумулятор|батаре\w*|ёмкост\w*|емкост\w*)[^%\d]{0,25}(\d{2,3})\s*%", text, re.I)
    if not m:
        m = re.search(r"(\d{2,3})\s*%\s*(?:акб|аккум|батар|ёмкост|емкост)", text, re.I)
    if m:
        val = int(m.group(1))
        if 50 <= val <= 100:
            return val
    return None


def passes(item, f):
    text = item["text"].lower()
    title = item["title"].lower()

    if not re.search(f["title_must_match"], title, re.I):
        return False, "не макбук"

    if not (f["price_min"] <= item["price"] <= f["price_max"]):
        return False, f"цена {item['price']}"

    for pattern in f["blacklist"]:
        if re.search(pattern, text, re.I):
            return False, f"стоп-слово: {pattern}"

    chip = f.get("chip_must_match")
    if chip and not re.search(chip, text, re.I):
        return False, "чип не Apple Silicon"

    ram = ram_gb(text)
    if f.get("require_ram_gb") and (ram is None or ram < f["require_ram_gb"]):
        return False, f"память {ram}"

    cyc = cycles(text)
    if cyc is not None and f.get("max_cycles") and cyc > f["max_cycles"]:
        return False, f"{cyc} циклов"

    bat = battery_pct(text)
    if bat is not None and f.get("min_battery_pct") and bat < f["min_battery_pct"]:
        return False, f"АКБ {bat}%"

    item["_ram"] = ram
    item["_cycles"] = cyc
    item["_battery"] = bat
    return True, "ок"



# ---------------------------------------------------------------- телеграм

def tg(token, method, params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log(f"телеграм отказал на {method}: {e.code} {e.read().decode('utf-8','replace')[:200]}")
    except OSError as e:
        log(f"телеграм недоступен: {e}")
    return {}


def escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_item(item, label):
    bits = []
    if item.get("_ram"):
        bits.append(f"{item['_ram']} ГБ")
    if item.get("_battery"):
        bits.append(f"АКБ {item['_battery']}%")
    if item.get("_cycles") is not None:
        bits.append(f"{item['_cycles']} циклов")
    spec = " · ".join(bits) if bits else "параметры не указаны"
    price = f"{item['price']:,}".replace(",", " ")
    return (f"<b>{escape(item['title'])}</b>\n{price} ₽\n{spec}\n"
            f"<i>{escape(label)}</i>\n{item['url']}")


# ------------------------------------------------- состояние в закреплённом сообщении

def state_load(token, chat):
    """Читает список показанных id из закреплённого сообщения чата."""
    res = tg(token, "getChat", {"chat_id": chat}).get("result") or {}
    pinned = res.get("pinned_message") or {}
    text = pinned.get("text") or ""
    if not text.startswith(STATE_MARKER):
        return set(), None
    try:
        payload = json.loads(text[len(STATE_MARKER):].strip())
        return set(payload), pinned.get("message_id")
    except ValueError:
        return set(), pinned.get("message_id")


def state_save(token, chat, seen, message_id):
    body = STATE_MARKER + "\n" + json.dumps(sorted(seen)[-STATE_KEEP:], ensure_ascii=False)
    if message_id:
        out = tg(token, "editMessageText",
                 {"chat_id": chat, "message_id": message_id, "text": body})
        if out.get("ok") or "message is not modified" in json.dumps(out):
            return message_id
    sent = tg(token, "sendMessage",
              {"chat_id": chat, "text": body, "disable_notification": "true"})
    new_id = ((sent.get("result") or {}).get("message_id"))
    if new_id:
        tg(token, "pinChatMessage",
           {"chat_id": chat, "message_id": new_id, "disable_notification": "true"})
    return new_id


# ---------------------------------------------------------------- точка входа

def handler(event, context):
    token = os.environ.get("TG_TOKEN", "")
    chat = os.environ.get("TG_CHAT", "")
    if ":" not in token or not chat:
        log("TG_TOKEN или TG_CHAT не заданы в переменных функции")
        return {"statusCode": 500, "body": "нет настроек"}

    searches = json.loads(os.environ["SEARCHES"]) if os.environ.get("SEARCHES") else DEFAULT_SEARCHES
    filters = dict(DEFAULT_FILTERS)
    if os.environ.get("FILTERS"):
        filters.update(json.loads(os.environ["FILTERS"]))

    seen, msg_id = state_load(token, chat)
    first_run = not seen
    fresh, parsed_total, blocked = [], 0, False

    for search in searches:
        try:
            page = fetch(search["url"])
            items = extract_items(page)
        except BlockedError as e:
            log(f"[{search['label']}] {e}")
            blocked = True
            continue
        except urllib.error.HTTPError as e:
            log(f"[{search['label']}] HTTP {e.code}")
            blocked = blocked or e.code in (403, 429)
            continue
        except OSError as e:
            log(f"[{search['label']}] сеть: {e}")
            continue

        parsed_total += len(items)
        log(f"[{search['label']}] объявлений на странице: {len(items)}")
        for item in items:
            ok, _ = passes(item, filters)
            if ok and item["id"] not in seen:
                seen.add(item["id"])
                fresh.append((item, search["label"]))
        time.sleep(3 + random.uniform(0, 2))

    if parsed_total == 0:
        reason = "Авито закрылся заглушкой или капчей" if blocked else "разметка выдачи изменилась"
        log("ничего не разобрано: " + reason)
        tg(token, "sendMessage", {"chat_id": chat, "text": f"⚠️ Парсер Авито не работает: {reason}"})
        return {"statusCode": 200, "body": "blocked"}

    if first_run:
        state_save(token, chat, seen, msg_id)
        tg(token, "sendMessage", {"chat_id": chat,
                                  "text": f"✅ Слежение запущено. Запомнил {len(seen)} текущих объявлений, "
                                          f"писать буду только про новые."})
        return {"statusCode": 200, "body": f"initialized {len(seen)}"}

    for item, label in fresh:
        tg(token, "sendMessage", {"chat_id": chat, "text": format_item(item, label),
                                  "parse_mode": "HTML"})
        time.sleep(1)

    if fresh:
        state_save(token, chat, seen, msg_id)

    log(f"новых подходящих: {len(fresh)}")
    return {"statusCode": 200, "body": f"sent {len(fresh)}"}
