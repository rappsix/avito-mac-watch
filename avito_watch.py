#!/usr/bin/env python3
"""
avito_watch.py — следит за объявлениями на Авито и присылает новые в Телеграм.

Запуск:
    python3 avito_watch.py --init      создать файл конфигурации
    python3 avito_watch.py --chatid    узнать свой chat_id (после /start боту)
    python3 avito_watch.py --test      разовый прогон без отправки, показать что нашлось
    python3 avito_watch.py             рабочий режим (его и вешаем на launchd)

Состояние и настройки лежат в ~/.avito-watch/
"""

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html import unescape as html_unescape


class BlockedError(RuntimeError):
    """Авито отдал капчу или заглушку вместо выдачи."""

HOME = os.path.expanduser("~/.avito-watch")
CONFIG_PATH = os.path.join(HOME, "config.json")
SEEN_PATH = os.path.join(HOME, "seen.json")
LOG_PATH = os.path.join(HOME, "watch.log")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

DEFAULT_CONFIG = {
    "telegram_token": "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER",
    "telegram_chat_id": "ВСТАВЬ_СЮДА_CHAT_ID",
    "searches": [
        {
            "label": "M2 Pro 32GB",
            "url": "https://www.avito.ru/moskva/noutbuki?q=macbook+pro+m2+pro+32&s=104"
        },
        {
            "label": "Pro 32GB общий",
            "url": "https://www.avito.ru/moskva/noutbuki?q=macbook+pro+32gb&s=104"
        }
    ],
    "filters": {
        "price_min": 90000,
        "price_max": 145000,
        "require_ram_gb": 32,
        "title_must_match": "macbook|макбук",
        "blacklist": [
            "полос", "битый пиксел", "замена экрана", "заменен экран", "замене экран",
            "разбит", "трещин", "не работает экран", "нет изображен",
            "mdm", "мдм", "только обмен", "обмен на", "оптом", "на запчаст",
            "под восстановление", "не включается", "залит", "после залит",
            "intel", r"\bi[579]\b", "radeon", "touch bar", "тачбар"
        ],
        "chip_must_match": r"\bm[1-5]\b",
        "max_cycles": 600,
        "min_battery_pct": 85
    },
    "request_delay_sec": 4
}


# ---------------------------------------------------------------- утилиты

def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


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

def tg_send(cfg, text):
    url = f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": cfg["telegram_chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }).encode()
    req = urllib.request.Request(url, data=payload)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log(f"телеграм отказал: {e.code} {e.read().decode('utf-8', 'replace')[:200]}")
    except OSError as e:
        log(f"телеграм недоступен: {e}")
    return None


def format_item(item, label):
    bits = []
    if item.get("_ram"):
        bits.append(f"{item['_ram']} ГБ")
    if item.get("_battery"):
        bits.append(f"АКБ {item['_battery']}%")
    if item.get("_cycles") is not None:
        bits.append(f"{item['_cycles']} циклов")
    spec = " · ".join(bits) if bits else "параметры не указаны"

    return (f"<b>{escape(item['title'])}</b>\n"
            f"{item['price']:,} ₽".replace(",", " ") + "\n"
            f"{spec}\n"
            f"<i>{escape(label)}</i>\n"
            f"{item['url']}")


def escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- режимы

def load_config():
    """
    Настройки берутся из ~/.avito-watch/config.json, а на сервере —
    из переменных окружения, чтобы токен не лежал в репозитории.
    """
    global SEEN_PATH
    cfg = load_json(CONFIG_PATH, None)

    token = os.environ.get("AVITO_TG_TOKEN")
    chat = os.environ.get("AVITO_TG_CHAT")
    if token and chat:
        cfg = cfg or json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["telegram_token"] = token
        cfg["telegram_chat_id"] = chat

    searches = os.environ.get("AVITO_SEARCHES")
    if searches and cfg:
        try:
            cfg["searches"] = json.loads(searches)
        except ValueError:
            log("AVITO_SEARCHES не разобрался как JSON — беру список из конфига")

    state = os.environ.get("AVITO_STATE")
    if state:
        SEEN_PATH = os.path.abspath(state)

    return cfg


def dump_page(page, label):
    """Сохраняет страницу, когда разбор не удался — чтобы было что чинить."""
    try:
        os.makedirs(HOME, exist_ok=True)
        safe = re.sub(r"[^\w-]+", "_", label)[:40]
        path = os.path.join(HOME, f"debug_{safe}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(page)
        log(f"страница сохранена для разбора: {path}")
    except OSError:
        pass


def cmd_init():
    if os.path.exists(CONFIG_PATH):
        log(f"конфигурация уже есть: {CONFIG_PATH}")
        return
    save_json(CONFIG_PATH, DEFAULT_CONFIG)
    os.chmod(CONFIG_PATH, 0o600)
    log(f"создан {CONFIG_PATH} — впиши туда токен и chat_id")


def cmd_chatid(cfg):
    url = f"https://api.telegram.org/bot{cfg['telegram_token']}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read())
    except OSError as e:
        log(f"не получилось: {e}")
        return
    ids = set()
    for upd in data.get("result", []):
        chat = (upd.get("message") or upd.get("channel_post") or {}).get("chat") or {}
        if chat.get("id"):
            ids.add((chat["id"], chat.get("username") or chat.get("title") or ""))
    if not ids:
        log("пусто. Напиши боту /start в Телеграме и запусти команду снова.")
    for cid, name in ids:
        log(f"chat_id = {cid}   ({name})")


def run(cfg, dry=False):
    seen = set(load_json(SEEN_PATH, []))
    first_run = not seen
    fresh = []
    parsed_total = 0

    for search in cfg["searches"]:
        try:
            html = fetch(search["url"])
        except urllib.error.HTTPError as e:
            log(f"[{search['label']}] Авито ответил {e.code} — пропускаю круг")
            continue
        except OSError as e:
            log(f"[{search['label']}] сеть недоступна: {e}")
            continue

        try:
            items = extract_items(html)
        except BlockedError as e:
            log(f"[{search['label']}] {e} — жду следующего круга")
            dump_page(html, search["label"])
            continue

        if not items:
            dump_page(html, search["label"])
        parsed_total += len(items)
        log(f"[{search['label']}] найдено объявлений: {len(items)}")

        for item in items:
            ok, reason = passes(item, cfg["filters"])
            if not ok:
                continue
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            fresh.append((item, search["label"]))

        time.sleep(cfg.get("request_delay_sec", 4) + random.uniform(0, 2))

    if parsed_total == 0:
        log("ВНИМАНИЕ: не разобрано ни одного объявления — вероятно, Авито поменял вёрстку "
            "или включил защиту. Скрипт нужно поправить.")
        if not dry and cfg.get("telegram_token", "").count(":"):
            tg_send(cfg, "⚠️ Парсер Авито ничего не нашёл. Возможно, сломалась разметка.")
        return

    if first_run:
        log(f"первый запуск: запомнил {len(seen)} объявлений, уведомления не шлю")
        save_json(SEEN_PATH, sorted(seen))
        if dry:
            for item, label in fresh[:10]:
                print("\n" + format_item(item, label))
        return

    log(f"подходящих новых: {len(fresh)}")
    for item, label in fresh:
        msg = format_item(item, label)
        if dry:
            print("\n" + msg)
        else:
            tg_send(cfg, msg)
            time.sleep(1)

    if not dry:
        save_json(SEEN_PATH, sorted(seen))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--chatid", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--dump", action="store_true", help="сохранить страницы выдачи в ~/.avito-watch/")
    args = ap.parse_args()

    if args.init:
        cmd_init()
        return

    cfg = load_config()
    if cfg is None:
        log(f"нет конфигурации. Запусти: python3 {sys.argv[0]} --init")
        sys.exit(1)

    if args.chatid:
        cmd_chatid(cfg)
        return

    if not args.test and ":" not in str(cfg.get("telegram_token", "")):
        log("токен не заполнен — правь ~/.avito-watch/config.json")
        sys.exit(1)

    run(cfg, dry=args.test)


if __name__ == "__main__":
    main()
