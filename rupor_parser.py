#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Парсер афиши rupor.events (клубные мероприятия).

Использование:
    python rupor_parser.py spb                     # вывод JSON в stdout
    python rupor_parser.py spb --csv events.csv    # плюс CSV
    python rupor_parser.py msk --json events.json  # другой город
    python rupor_parser.py spb --genre techno      # фильтр по жанру (подстрока)

Извлекаемые поля: date, weekday, title, url, venue, genres, poster.

Парсер не привязан к CSS-классам: опирается на структуру
(заголовки-дни <h2>, ссылки /event/, иконку place.svg перед названием площадки),
поэтому переживает косметические изменения вёрстки.
"""

import argparse
import csv
import datetime as dt
import json
import re
import sys

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

BASE_URL = "https://rupor.events"

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

# «пятница 3 июля»
DAY_HEADER_RE = re.compile(
    r"^(понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)\s+(\d{1,2})\s+([а-яё]+)$",
    re.IGNORECASE,
)

# дата в слаге события: /event/2026-07-03-...
URL_DATE_RE = re.compile(r"/event/(\d{4})-(\d{2})-(\d{2})-")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def fetch_html(city: str, session: requests.Session | None = None) -> str:
    s = session or requests.Session()
    resp = s.get(f"{BASE_URL}/{city}", headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def resolve_year(day: int, month: int, ref: dt.date) -> int:
    """Заголовки дней не содержат год — подбираем ближайший будущий.

    Афиша отсортирована от сегодня вперёд, поэтому дата раньше чем
    (ref - небольшой зазор) означает переход через Новый год.
    """
    candidate = dt.date(ref.year, month, day)
    # зазор в 7 дней: события «сегодня/вчера» не должны улетать на год вперёд
    if candidate < ref - dt.timedelta(days=7):
        return ref.year + 1
    return ref.year


# «Суббота, 4 июля, 17:00» на странице события
DATETIME_RE = re.compile(r"(\d{1,2})\s+([а-яё]+)\s*,?\s*(\d{1,2}):(\d{2})", re.IGNORECASE)


def fetch_event_page(url: str, session: requests.Session,
                     cache_dir: str | None = None) -> str:
    """GET страницы события с дисковым кэшем (по слагу)."""
    if cache_dir:
        import hashlib
        import os
        os.makedirs(cache_dir, exist_ok=True)
        slug = url.rstrip("/").rsplit("/", 1)[-1][:80]
        path = os.path.join(
            cache_dir, slug + "-" + hashlib.md5(url.encode()).hexdigest()[:8] + ".html")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    resp = session.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    if cache_dir:
        with open(path, "w", encoding="utf-8") as f:
            f.write(resp.text)
    return resp.text


def _details_from_jsonld(soup: BeautifulSoup) -> dict:
    """schema.org Event из <script type=application/ld+json> — основной источник."""
    out: dict = {}
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            t = item.get("@type", "")
            if "Event" not in (t if isinstance(t, str) else " ".join(t)):
                continue
            if item.get("startDate"):
                out["start"] = item["startDate"]
            if item.get("endDate"):
                out["end"] = item["endDate"]
            if item.get("description"):
                out["description"] = item["description"].strip()
            loc = item.get("location") or {}
            if isinstance(loc, dict):
                addr = loc.get("address")
                if isinstance(addr, dict):
                    parts = [addr.get(k) for k in
                             ("addressLocality", "streetAddress") if addr.get(k)]
                    if parts:
                        out["address"] = ", ".join(parts)
                elif isinstance(addr, str) and addr:
                    out["address"] = addr
            offers = item.get("offers") or {}
            if isinstance(offers, dict) and offers.get("price") is not None:
                out["price"] = offers["price"]
            return out
    return out


def parse_event_page(html: str, base_year: int) -> dict:
    """Детали события: точное время начала/конца, адрес, описание, цена.

    Основной источник — JSON-LD (schema.org Event) в <head>.
    Фолбэк — обход DOM по якорям:
      - img Calendar.svg -> текст с датой/временем («Суббота, 4 июля, 17:00 – ...»)
      - ссылка /venue/   -> следующий текст = адрес
      - описание = текст после адреса/жанров до <h2>Обновлено</h2>
    """
    soup = BeautifulSoup(html, "html.parser")
    jsonld = _details_from_jsonld(soup)
    body = soup.body or soup
    out: dict = {"start": None, "end": None, "address": None, "description": None}
    out.update(jsonld)
    if all(out.get(k) for k in ("start", "address", "description")):
        return out

    STATE_SEEK_CAL, STATE_DATETIME, STATE_ADDRESS, STATE_DESC, STATE_DONE = range(5)
    state = STATE_SEEK_CAL
    dt_text: list[str] = []
    desc_parts: list[str] = []
    last_p = None
    venue_seen = False

    for node in body.descendants:
        if isinstance(node, Comment):
            continue

        if isinstance(node, Tag):
            if node.name == "img" and "Calendar.svg" in node.get("src", "") \
                    and state == STATE_SEEK_CAL:
                state = STATE_DATETIME
            elif node.name == "a" and "/venue/" in node.get("href", "") \
                    and state == STATE_DATETIME:
                state = STATE_ADDRESS
                venue_seen = True
            elif node.name == "h2" and state in (STATE_ADDRESS, STATE_DESC):
                if node.get_text(strip=True).lower().startswith("обновлено"):
                    state = STATE_DONE
                    break
            elif node.name == "h2" and state == STATE_DATETIME:
                break  # событие без карточки площадки: дальше данных нет
            elif node.name == "br" and state == STATE_DESC:
                desc_parts.append("\n")
            continue

        if not isinstance(node, NavigableString):
            continue
        # содержимое <script>/<style> — не текст страницы
        if isinstance(node.parent, Tag) and node.parent.name in ("script", "style"):
            continue
        text = " ".join(node.split())
        if not text:
            continue

        if state == STATE_DATETIME:
            dt_text.append(text)
        elif state == STATE_ADDRESS:
            # пропускаем название площадки внутри самой ссылки /venue/
            pa = node.find_parent("a")
            if pa and "/venue/" in pa.get("href", ""):
                continue
            if not out["address"]:
                out["address"] = text
            state = STATE_DESC
        elif state == STATE_DESC:
            # жанровые теги не относятся к описанию
            parent = node.parent
            classes = parent.get("class", []) if isinstance(parent, Tag) else []
            if any("btn-tag" in c for c in classes):
                continue
            p = node.find_parent("p")
            if desc_parts and p is not last_p:
                desc_parts.append("\n\n")
            last_p = p
            if desc_parts and desc_parts[-1] not in ("\n", "\n\n"):
                desc_parts.append(" ")
            desc_parts.append(text)

    # если /venue/ не встретилась (нет карточки клуба) — дата могла собраться,
    # но адрес/описание не найдены; это нормально для part-событий
    if (venue_seen or dt_text) and not out["start"]:
        matches = DATETIME_RE.findall(" ".join(dt_text))
        stamps = []
        for day_s, month_s, hh, mm in matches[:2]:
            month = MONTHS.get(month_s.lower())
            if not month:
                continue
            year = base_year
            stamps.append(dt.datetime(year, month, int(day_s), int(hh), int(mm)))
        if stamps:
            out["start"] = stamps[0].isoformat(timespec="minutes")
            if len(stamps) > 1:
                end = stamps[1]
                if end < stamps[0]:  # переход через Новый год
                    end = end.replace(year=end.year + 1)
                out["end"] = end.isoformat(timespec="minutes")

    if not out["description"]:
        desc = "".join(desc_parts).strip()
        out["description"] = desc or None
    return out


def enrich_with_details(events: list[dict], delay: float = 0.4,
                        cache_dir: str | None = None,
                        limit: int | None = None) -> None:
    """Второй проход: подтягивает start/end/address/description со страниц событий."""
    import time
    session = requests.Session()
    details_by_url: dict[str, dict] = {}
    urls = []
    for e in events:
        if e["url"] not in details_by_url:
            details_by_url[e["url"]] = {}
            urls.append(e["url"])
    if limit:
        urls = urls[:limit]

    for i, url in enumerate(urls, 1):
        try:
            html = fetch_event_page(url, session, cache_dir)
            year = int(next(e for e in events if e["url"] == url)["date"][:4])
            details_by_url[url] = parse_event_page(html, year)
        except Exception as exc:  # noqa: BLE001 — одна битая страница не валит прогон
            print(f"[{i}/{len(urls)}] FAIL {url}: {exc}", file=sys.stderr)
            continue
        print(f"[{i}/{len(urls)}] ok {url}", file=sys.stderr)
        if delay and i < len(urls):
            time.sleep(delay)

    for e in events:
        e.update({k: v for k, v in details_by_url.get(e["url"], {}).items() if v})


def parse_events(html: str, ref_date: dt.date | None = None) -> list[dict]:
    ref = ref_date or dt.date.today()
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body or soup

    events: list[dict] = []
    seen: set[tuple[str, str]] = set()

    current_date: dt.date | None = None
    current_weekday: str | None = None
    current: dict | None = None       # карточка, которую сейчас наполняем
    after_place_icon = False          # следующий текст — название площадки
    pending_poster: dict[str, str] = {}  # url события -> постер (ссылка-картинка идёт раньше)

    def flush():
        nonlocal current, after_place_icon
        if current:
            key = (current["url"], current["date"] or "")
            if key not in seen:
                seen.add(key)
                events.append(current)
        current = None
        after_place_icon = False

    for node in body.descendants:
        # --- заголовок дня ---
        if isinstance(node, Tag) and node.name == "h2":
            header_text = " ".join(node.get_text(" ", strip=True).split()).lower()
            m = DAY_HEADER_RE.match(header_text)
            flush()
            if m:
                weekday, day_s, month_s = m.groups()
                month = MONTHS.get(month_s)
                if month:
                    day = int(day_s)
                    current_date = dt.date(resolve_year(day, month, ref), month, day)
                    current_weekday = weekday
                else:
                    current_date = None
            else:
                # «Выбор редакции», «Модная клубная одежда» и т.п.
                current_date = None
                current_weekday = None
            continue

        # --- ссылка на событие ---
        if isinstance(node, Tag) and node.name == "a":
            href = node.get("href", "")
            if "/event/" not in href:
                continue
            url = href if href.startswith("http") else BASE_URL + href
            title = " ".join(node.get_text(" ", strip=True).split())
            img = node.find("img")

            if title:  # текстовая ссылка = название события, начинаем карточку
                flush()
                if current_date is None:
                    continue  # секции вне календаря пропускаем (дубли из «Выбора редакции»)
                current = {
                    "date": current_date.isoformat(),
                    "weekday": current_weekday,
                    "title": title,
                    "url": url,
                    "venue": None,
                    "genres": [],
                    "poster": pending_poster.pop(url, None),
                }
                if node.get("data-id"):
                    current["id"] = node["data-id"]
                mdate = URL_DATE_RE.search(url)
                if mdate:
                    current["url_date"] = "-".join(mdate.groups())
            elif img and img.get("src"):
                # ссылка-постер идёт перед текстовой ссылкой того же события
                pending_poster[url] = img["src"]
            continue

        # --- иконка площадки ---
        if isinstance(node, Tag) and node.name == "img":
            src = node.get("src", "")
            if "place.svg" in src and current is not None:
                after_place_icon = True
            continue

        # --- текстовые узлы внутри карточки: жанры и площадка ---
        if isinstance(node, NavigableString) and current is not None:
            if isinstance(node, Comment):
                continue
            text = node.strip()
            if not text:
                continue
            # текст самой ссылки-названия уже учтён
            parent_a = node.find_parent("a") if hasattr(node, "find_parent") else None
            if parent_a and "/event/" in parent_a.get("href", ""):
                continue
            if after_place_icon:
                current["venue"] = " ".join(text.split())
                after_place_icon = False
                flush()
            else:
                # жанры лежат в <span class="btn btn-tag btn-tag-...">
                parent = node.parent
                classes = parent.get("class", []) if isinstance(parent, Tag) else []
                if any("btn-tag" in c for c in classes):
                    current["genres"].append(" ".join(text.split()))

    flush()
    return events


def parse_date_arg(s: str, ref: dt.date | None = None) -> dt.date:
    """--date: 2026-07-04 | 04.07.2026 | 4.07 | сегодня | завтра | 4 июля"""
    ref = ref or dt.date.today()
    s = s.strip().lower()
    if s in ("сегодня", "today"):
        return ref
    if s in ("завтра", "tomorrow"):
        return ref + dt.timedelta(days=1)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", s)
    if m:
        d, mo = int(m[1]), int(m[2])
        y = int(m[3]) if m[3] else resolve_year(d, mo, ref)
        return dt.date(y, mo, d)
    m = re.fullmatch(r"(\d{1,2})\s+([а-яё]+)", s)
    if m and MONTHS.get(m[2]):
        d, mo = int(m[1]), MONTHS[m[2]]
        return dt.date(resolve_year(d, mo, ref), mo, d)
    raise ValueError(f"Не понял дату: {s!r}")


def default_output_dir() -> str:
    """Каталог для файлов по умолчанию: Downloads телефона (Termux), иначе cwd."""
    import os
    candidates = (
        "/storage/emulated/0/Download",
        os.path.expanduser("~/storage/downloads"),
    )
    for p in candidates:
        if os.path.isdir(p) and os.access(p, os.W_OK):
            return p
    return "."


def write_txt(events: list[dict], path: str) -> None:
    """Человекочитаемый TXT: по блоку на событие."""
    lines: list[str] = []
    for e in events:
        lines.append("=" * 60)
        lines.append(e["title"])
        if e.get("start"):
            when = e["start"][:16].replace("T", " ")
            if e.get("end"):
                when += " – " + e["end"][:16].replace("T", " ")
            lines.append(f"Когда:  {when}")
        else:
            lines.append(f"Когда:  {e['date']} ({e['weekday']})")
        place = e.get("venue") or ""
        if e.get("address"):
            place += f" — {e['address']}" if place else e["address"]
        if place:
            lines.append(f"Где:    {place}")
        if e["genres"]:
            lines.append(f"Жанры:  {', '.join(e['genres'])}")
        if e.get("price") is not None:
            lines.append(f"Цена:   от {e['price']} ₽")
        lines.append(f"Ссылка: {e['url']}")
        if e.get("description"):
            lines.append("")
            lines.append(e["description"])
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser(description="Парсер афиши rupor.events")
    ap.add_argument("city", nargs="?", default="spb", help="слаг города (spb, msk, ...)")
    ap.add_argument("--json", metavar="FILE", help="сохранить JSON в файл")
    ap.add_argument("--csv", metavar="FILE", help="сохранить CSV в файл")
    ap.add_argument("--genre", metavar="SUBSTR", help="фильтр по жанру (подстрока, без регистра)")
    ap.add_argument("--date", metavar="DATE",
                    help="только события этой даты: 2026-07-04 | 4.07 | 4 июля | сегодня | завтра; "
                         "автоматически включает --details")
    ap.add_argument("--from-file", metavar="HTML", help="парсить локальный HTML вместо запроса")
    ap.add_argument("--details", action="store_true",
                    help="догрузить страницы событий: время начала/конца, адрес, описание")
    ap.add_argument("--delay", type=float, default=0.4,
                    help="пауза между запросами страниц событий, сек (по умолчанию 0.4)")
    ap.add_argument("--cache", metavar="DIR", default=".rupor_cache",
                    help="каталог кэша страниц событий (по умолчанию .rupor_cache)")
    ap.add_argument("--limit", type=int, help="ограничить число догружаемых событий (для отладки)")
    ap.add_argument("--txt", metavar="FILE",
                    help="экспорт в читаемый TXT (в режиме даты создаётся автоматически)")
    ap.add_argument("--no-ask", action="store_true",
                    help="не спрашивать дату интерактивно, парсить всю афишу")
    ap.add_argument("--outdir", metavar="DIR",
                    help="каталог для выходных файлов (по умолчанию Downloads телефона, если доступен)")
    args = ap.parse_args()

    import os
    outdir = args.outdir or default_output_dir()

    def out_path(p: str) -> str:
        # явный путь с каталогом не трогаем, голое имя кладём в outdir
        return p if os.path.dirname(p) else os.path.join(outdir, p)

    # интерактивный запрос даты, если не передана флагом
    if not args.date and not args.no_ask and sys.stdin.isatty():
        try:
            raw = input("Дата (2026-07-04 | 4.07 | 4 июля | завтра) [сегодня]: ").strip()
        except EOFError:
            raw = ""
        args.date = raw or "сегодня"

    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            html = f.read()
    else:
        html = fetch_html(args.city)

    events = parse_events(html)

    if args.genre:
        needle = args.genre.lower()
        events = [e for e in events if any(needle in g.lower() for g in e["genres"])]

    if args.date:
        target = parse_date_arg(args.date).isoformat()
        events = [e for e in events if e["date"] == target]
        args.details = True
        if not args.txt:
            args.txt = f"rupor_{args.city}_{target}.txt"
        print(f"Дата {target}: событий {len(events)}", file=sys.stderr)

    if args.details:
        enrich_with_details(events, delay=args.delay, cache_dir=args.cache,
                            limit=args.limit)

    if args.json:
        args.json = out_path(args.json)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)

    if args.csv:
        args.csv = out_path(args.csv)
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["date", "start", "end", "weekday", "title", "venue",
                        "address", "genres", "description", "url"])
            for e in events:
                w.writerow([e["date"], e.get("start", ""), e.get("end", ""),
                            e["weekday"], e["title"], e["venue"] or "",
                            e.get("address", ""), ", ".join(e["genres"]),
                            e.get("description", ""), e["url"]])

    if args.txt:
        args.txt = out_path(args.txt)
        write_txt(events, args.txt)
        print(f"TXT: {args.txt}", file=sys.stderr)

    if not args.json and not args.csv and not args.txt:
        json.dump(events, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(f"Событий: {len(events)}", file=sys.stderr)


if __name__ == "__main__":
    main()
