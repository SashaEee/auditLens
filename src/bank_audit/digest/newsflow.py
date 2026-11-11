"""Полный поток новостей «Обзора»: сбор каждые 20 минут, полный текст, два шага отбора.

Аудит 24.09.2026 показал, что старый пул видел 5–10 процентов потока: один сбор
в 07:00, 40 мест на 13 источников, по 10 последних записей с источника. От
Banksta (~100 постов в сутки) и Frank Media (~40) в пул попадали 4–5 ночных
постов — эвтаназия в Канаде, тесты PISA, — а сильные поводы дня («Росфинмониторинг
предлагает новое основание блокировки счетов», «ЦБ обсуждает полный возврат
похищенного») не доходили даже до кандидатов. Отбор к тому же шёл по заголовку
и 180 знакам, а рубрика считала ставки конкурентов «релевантными на 6–10».

Здесь:
  • сбор — все источники каждые 20 минут, Telegram листается назад до уже
    прочитанного (до 48 ч), сводные посты («Важные новости, которые вы могли
    пропустить») разбираются на отдельные новости, заголовок — осмысленная
    строка, а не эмодзи и хэштеги;
  • ступень 1 — дешёвая модель по ВСЕМУ потоку: касается ли розничного
    банковского бизнеса и какого типа событие;
  • склейка — один и тот же сюжет из разных источников становится одним
    событием (векторы bge-m3), число источников — сигнал важности;
  • ступень 2 — сильная модель читает полный текст ведущей записи события и
    оценивает его как руководитель аудита розницы: повод ли это для проверки,
    какой процесс Сбера затронут, что проверить и что запросить, есть ли срок.
    Ставки, тарифы и макроэкономика — «фон рынка», а не поводы.

Схема: migrations/071_news_flow.sql.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from .. import db
from . import news as nm

log = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
S1_MODEL = os.getenv("NEWSFLOW_S1_MODEL", "google/gemini-3-flash-preview")
S2_MODEL = os.getenv("NEWSFLOW_S2_MODEL", "anthropic/claude-opus-4.8")
LOOKBACK_H = float(os.getenv("NEWSFLOW_LOOKBACK_H", "48"))
TG_PAGES = int(os.getenv("NEWSFLOW_TG_PAGES", "12"))
S2_MIN_REL = int(os.getenv("NEWSFLOW_S2_MIN_REL", "6"))
EVENT_SIM = float(os.getenv("NEWSFLOW_EVENT_SIM", "0.86"))
BODY_CHARS = int(os.getenv("NEWSFLOW_BODY_CHARS", "6000"))
# потолок трат ступени 2 за сутки — предохранитель от зацикливания
S2_DAILY_CAP = int(os.getenv("NEWSFLOW_S2_DAILY_EVENTS", "260"))

# ── Заголовок и сводные посты ────────────────────────────────────────────────
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200d\u20E3"
                    "\U0001F1E6-\U0001F1FF\u2190-\u21FF\u25A0-\u25FF"
                    "\u203C\u2049\u2122\u2139\u3030\u303D]")
_HASHTAG = re.compile(r"(^|\s)#[\w\d_]+")
_ROUNDUP = re.compile(r"могли пропустить|главное за (день|неделю|сутки)|главные новости|"
                      r"коротко о главном|дайджест", re.I)
# Обзор или дайджест недели ведущей записью события — плохой выбор: длинный
# текст обо всём сразу, модель оценивала его «в среднем» (27.09 «Главное о
# кредитах за неделю» — 4 из 10, хотя внутри решение ЦБ).
_DIGEST_TITLE = re.compile(r"дайджест|могли пропустить|итоги недели|обзор недели|"
                           r"главное (?:о|об|за|по)\b.{0,40}(?:недел|сутки|день|месяц)", re.I)
_BULLET = re.compile(r"^\s*(?:[\U0001F000-\U0001FAFF☀-➿←-⇿■-◿"
                     r"️•·▪–—\-]+|\d{1,2}[.)])\s*")


def letters(s: str) -> int:
    return sum(ch.isalpha() for ch in s or "")


def clean_line(s: str) -> str:
    s = _EMOJI.sub(" ", s or "")
    s = _HASHTAG.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" -—:|•·▪")
    # хвосты оформления в начале строки: «!!!», стрелки, маркеры
    s = re.sub(r"^[^\w«\"(]+", "", s)
    return s


def clean_title(text_: str, fallback: str = "") -> str:
    """Первая осмысленная строка: не эмодзи, не хэштеги, не подпись канала.
    Раньше заголовком шла первая строка поста — и в выпуск попадали
    «🔤 🔤 🔤» и «#БанковскийСектор»."""
    for line in (text_ or "").split("\n"):
        c = clean_line(line)
        if letters(c) >= 20 and not re.match(r"(?i)^(подписаться|читать|источник|frank media в)", c):
            return c[:200]
    c = clean_line(" ".join((text_ or fallback or "").split("\n")))
    return c[:160]


def title_ok(t: str) -> bool:
    """Годен ли заголовок для выпуска: осмысленный текст, не хэштег, не эмодзи."""
    t = (t or "").strip()
    return letters(t) >= 20 and not t.startswith("#")


def split_roundup(item: dict) -> list[dict]:
    """Сводный пост («Важные новости… которые вы могли пропустить вчера») —
    десяток новостей в одном посте; целиком он получает низкую оценку и
    пропадает. Разбираем на отдельные пункты."""
    body = item.get("body") or ""
    if not _ROUNDUP.search(body[:300]):
        return []
    out = []
    for n, line in enumerate(body.split("\n"), 1):
        if not _BULLET.match(line):
            continue
        c = clean_line(_BULLET.sub("", line))
        if letters(c) < 30:
            continue
        out.append({**item, "url": f"{item['url']}#{n}", "parent_url": item["url"],
                    "title": c[:200], "body": c, "image": None})
    return out if len(out) >= 3 else []


# ── Сбор ─────────────────────────────────────────────────────────────────────
_TG_TEXT = re.compile(r'tgme_widget_message_text[^>]*>(.*?)</div>', re.S)


def _parse_tg_page(html_text: str, src: dict) -> list[dict]:
    items = []
    for block in html_text.split("tgme_widget_message_wrap")[1:]:
        pm = re.search(r'data-post="([^"]+)"', block)
        tm = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        if not pm or not tm:
            continue
        texts = _TG_TEXT.findall(block)
        body = nm._strip_html((texts[-1] if texts else "").replace("<br/>", "\n").replace("<br>", "\n"))
        body = re.sub(r"[ \t]+", " ", body).strip()
        try:
            pid = int(pm.group(1).rsplit("/", 1)[-1])
        except ValueError:
            continue
        img = nm._TG_IMG_RE.search(block)
        items.append({"pid": pid, "url": f"https://t.me/{pm.group(1)}",
                      "ts": nm._parse_dt(tm.group(1)), "body": body[:8000],
                      "image": img.group(1) if img else None})
    return items


def _fetch_tg(src: dict, since: datetime, last_post: int | None) -> tuple[list[dict], int | None]:
    """Посты канала с листанием назад: до уже прочитанного поста или до окна."""
    out: dict[int, dict] = {}
    before = None
    for _ in range(TG_PAGES):
        r = nm._get(src["url"] + (f"?before={before}" if before else ""))
        if r.status_code != 200:
            break
        page = _parse_tg_page(r.text, src)
        if not page:
            break
        for p in page:
            out[p["pid"]] = p
        before = min(p["pid"] for p in page)
        oldest = min((p["ts"] for p in page if p["ts"]), default=None)
        if (last_post and before <= last_post) or (oldest and oldest < since):
            break
    posts = [p for p in out.values() if p["ts"] and p["ts"] >= since and len(p["body"]) >= 25]
    return posts, (max(out) if out else last_post)


# ── Страницы со списком публикаций (исследования) ────────────────────────────
# У сайтов исследований лент нет: следим за страницей, где публикации перечислены
# ссылками. Новым считается то, чего ещё нет в news_item. Всё, что лежало на
# странице при первом проходе, запоминается «старым» — иначе в день подключения
# в поток ушла бы сотня докладов прошлых лет как свежие новости.
PAGE_OLD_DAYS = 60       # на сколько дней назад относим то, что было на сайте до нас
PAGE_NEW_MAX = 8         # больше новых ссылок без даты за проход — это смена адресов
_A_RE = re.compile(r'<a\b[^>]*?href="([^"#][^"]*)"[^>]*>(.*?)</a>', re.S | re.I)
_RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря")
_D_NUM = re.compile(r"(?<!\d)(\d{1,2})\.(\d{2})\.(20\d\d)(?!\d)")
_D_RU = re.compile(r"(?<!\d)(\d{1,2})\s+(" + "|".join(_RU_MONTHS) + r")\s+(20\d\d)", re.I)
_D_URL = re.compile(r"(?<!\d)(20\d\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")


def _dates_in(text_: str) -> list[date]:
    """Даты в тексте по порядку появления: «23.09.2026» и «23 сентября 2026»."""
    hits = []
    for m in _D_NUM.finditer(text_ or ""):
        hits.append((m.start(), int(m.group(3)), int(m.group(2)), int(m.group(1))))
    for m in _D_RU.finditer(text_ or ""):
        hits.append((m.start(), int(m.group(3)), _RU_MONTHS.index(m.group(2).lower()) + 1,
                     int(m.group(1))))
    out = []
    for _pos, y, mo, d in sorted(hits):
        try:
            out.append(date(y, mo, d))
        except ValueError:
            continue
    return out


def parse_listing(page: str, page_url: str, src: dict) -> list[dict]:
    """Публикации на странице-списке: ссылки, чей путь подходит под src["link"].
    Дата — из адреса (…_20260821_…), иначе из текста рядом со ссылкой с той
    стороны, которую назвал источник (src["date"]: before | after). Без этого
    указания соседний текст не читаем: дата следующей карточки стоит так же
    близко, как своя."""
    from urllib.parse import urljoin, urlparse
    host = urlparse(page_url).netloc.removeprefix("www.")
    link = re.compile(src["link"])
    hits = []
    for m in _A_RE.finditer(page or ""):
        url = urljoin(page_url, html.unescape(m.group(1)).strip())
        pu = urlparse(url)
        if pu.netloc.removeprefix("www.") != host or not link.search(pu.path):
            continue
        hits.append((m, url, pu.path))
    out, seen = [], set()
    for k, (m, url, path) in enumerate(hits):
        title = clean_line(nm._strip_html(m.group(2)))
        if url in seen or letters(title) < 12:
            continue
        seen.add(url)
        d = None
        um = _D_URL.search(path)
        if um:
            try:
                d = date(int(um.group(1)), int(um.group(2)), int(um.group(3)))
            except ValueError:
                d = None
        side = src.get("date")
        if d is None and side == "before":
            lo = max(hits[k - 1][0].end() if k else 0, m.start() - 900)
            ds = _dates_in(nm._strip_html(page[lo:m.start()]))
            d = ds[-1] if ds else None
        elif d is None and side == "after":
            hi = min(hits[k + 1][0].start() if k + 1 < len(hits) else len(page), m.end() + 900)
            ds = _dates_in(nm._strip_html(page[m.end():hi]))
            d = ds[0] if ds else None
        # пояснение под заголовком — до следующей публикации; оборванный на
        # середине тег отбрасываем, а где дата замыкает карточку — режем по дате
        hi = min(next((h[0].start() for h in hits[k + 1:] if h[1] != url), len(page)), m.end() + 1200)
        note = nm._strip_html(re.sub(r"<[^>]*$", "", page[m.end():hi]))
        if side == "after":
            dm = min((x.start() for x in (_D_NUM.search(note), _D_RU.search(note)) if x), default=None)
            note = note[:dm] if dm is not None else ""
        note = clean_line(note)[:320]
        if letters(note) < 40 or note[:40].lower() == title[:40].lower():
            note = ""
        out.append({"url": url, "title": title[:300], "date": d, "note": note})
    return out


def page_rows(src: dict, found: list[dict], known: set[str], boot: bool,
              now: datetime | None = None) -> list[dict]:
    """Строки для записи: только то, чего ещё нет. Время публикации: дата со
    страницы (сегодняшняя — время находки); без даты — время находки, а при
    первом проходе и при подозрении на смену адресов — «давно»."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(MSK).date()
    fresh = [f for f in found if nm._url_hash(f["url"]) not in known]
    old_mode = boot or sum(1 for f in fresh if not f["date"]) > PAGE_NEW_MAX
    if old_mode and not boot and fresh:
        log.warning("newsflow.page: %s — %d новых ссылок без даты за проход, считаю старыми "
                    "(похоже на смену адресов)", src["key"], len(fresh))
    base = now - timedelta(days=PAGE_OLD_DAYS)
    rows = []
    for i, f in enumerate(fresh):
        d = f["date"]
        if d:
            ts = now if d >= today else datetime(d.year, d.month, d.day, 12, tzinfo=MSK)
        else:
            ts = base - timedelta(minutes=i) if old_mode else now
        rows.append(_row(src, f["url"], ts, f["title"], f.get("note") or f["title"]))
    return rows


def _page_known(hashes: list[str]) -> set[str]:
    """Какие из найденных ссылок уже записаны."""
    if not hashes:
        return set()
    with db.session() as s:
        return {r[0] for r in s.execute(
            text("SELECT url_hash FROM news_item WHERE url_hash = ANY(:h)"), {"h": hashes}).all()}


def _fetch_page(src: dict) -> tuple[list[dict], str | None]:
    """Публикации со страниц источника и ошибка, если какая-то страница не
    прочиталась или ссылок на ней не нашлось (вёрстка изменилась).

    «Первый проход» определяется по каждой странице отдельно: ни одна её ссылка
    ещё не записана. Так страница, не открывшаяся в день подключения, при
    первом удачном чтении тоже запомнится как старое, а не уйдёт в поток."""
    pages, errs = [], []
    for pu in src.get("pages") or [src["url"]]:
        try:
            r = nm._get(pu)
            if r.status_code != 200:
                raise RuntimeError(f"http {r.status_code}")
            got = parse_listing(r.text, pu, src)
            if not got:
                raise RuntimeError("публикаций на странице не найдено")
            pages.append(got)
        except Exception as e:  # noqa: BLE001 — одна страница, не источник целиком
            errs.append(f"{pu.rsplit('/', 2)[-2] or pu}: {type(e).__name__}: {str(e)[:80]}")
    if not pages:
        return [], "; ".join(errs)[:200] or "публикаций не найдено"
    known = _page_known([nm._url_hash(f["url"]) for got in pages for f in got])
    rows, now = [], datetime.now(timezone.utc)
    for got in pages:
        boot = not any(nm._url_hash(f["url"]) in known for f in got)
        rows += page_rows(src, got, known, boot, now)
    # одна публикация может стоять на двух страницах источника
    uniq = {r["h"]: r for r in reversed(rows)}
    return list(reversed(uniq.values())), ("; ".join(errs)[:200] or None)


def _row(src: dict, url: str, ts, title: str, body: str, image=None, parent=None) -> dict:
    return {"h": nm._url_hash(url), "u": url[:800], "pu": parent, "s": src["key"],
            "tag": src.get("tag"), "cls": src.get("cls", "bank"), "dim": src.get("dimension"),
            "ts": ts, "t": (title or "")[:300], "b": (body or "")[:BODY_CHARS], "img": image}


_INS = text("""
    INSERT INTO news_item (url_hash, url, parent_url, source, tag, cls, dimension, ts,
                           title, body, image)
    VALUES (:h, :u, :pu, :s, :tag, :cls, :dim, :ts, :t, :b, :img)
    ON CONFLICT (url_hash) DO NOTHING
""")


_STATE_UPSERT = text("""
    INSERT INTO news_source_state (source, last_ok_at, last_error, last_post, items_24h)
    VALUES (:s, CASE WHEN CAST(:e AS text) IS NULL THEN now() END, CAST(:e AS text),
            CAST(:lp AS bigint),
            (SELECT count(*) FROM news_item WHERE source = :s AND ts > now() - interval '24 hours'))
    ON CONFLICT (source) DO UPDATE SET
        last_ok_at = coalesce(EXCLUDED.last_ok_at, news_source_state.last_ok_at),
        last_error = EXCLUDED.last_error,
        last_post = coalesce(EXCLUDED.last_post, news_source_state.last_post),
        items_24h = EXCLUDED.items_24h
""")


def collect() -> dict:
    """Один проход сбора по всем источникам. Идемпотентен: уже виденное не
    дублируется. Сбой источника — запись в news_source_state, не падение."""
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_H)
    with db.session() as s:
        state = {r[0]: r[1] for r in s.execute(text(
            "SELECT source, last_post FROM news_source_state")).all()}
    added, per = 0, {}
    for src in nm.SOURCES:
        rows, err, last_post = [], None, state.get(src["key"])
        try:
            if src["kind"] == "tg":
                posts, last_post = _fetch_tg(src, since, state.get(src["key"]))
                for p in posts:
                    subs = split_roundup({"url": p["url"], "body": p["body"]})
                    if subs:            # сводный пост — только его пункты, без самой сводки
                        for sub in subs:
                            rows.append(_row(src, sub["url"], p["ts"], sub["title"], sub["body"],
                                             None, p["url"]))
                        continue
                    rows.append(_row(src, p["url"], p["ts"], clean_title(p["body"]),
                                     p["body"], p.get("image")))
            elif src["kind"] == "page":
                rows, err = _fetch_page(src)
            else:
                r = nm._get(src["url"])
                if r.status_code != 200:
                    raise RuntimeError(f"http {r.status_code}")
                for it in nm._parse_rss(r.text, src):
                    if it.get("ts") and it["ts"] < since:
                        continue
                    if src.get("title_join"):
                        t = " ".join((it.get("title") or "").split())[:300]
                    else:
                        t = clean_title(it.get("title") or "", it.get("snippet") or "")
                    rows.append(_row(src, it["url"], it.get("ts"), t,
                                     it.get("snippet") or "", it.get("image")))
        except Exception as e:  # noqa: BLE001 — источник, не сбор целиком
            err = f"{type(e).__name__}: {str(e)[:160]}"
        n = 0
        if rows:
            with db.session() as s:
                res = s.execute(_INS, rows)
                n = res.rowcount if res.rowcount and res.rowcount > 0 else 0
        added += n
        per[src["key"]] = {"new": n, "err": err}
        with db.session() as s:
            s.execute(_STATE_UPSERT, {"s": src["key"], "e": err, "lp": last_post})
    return {"added": added, "sources": per}


# ── Поиск по теме: происшествия со Сбером ─────────────────────────────────────
# Ленты «Обзора» — федеральные издания и их рубрики. Региональные сюжеты о Сбере
# (хищения в регионах, дела против сотрудников) в них не попадают:
# «Татарстан, 60 млн через ипотеку» 05.10 вышла в восьми региональных изданиях и
# прошла мимо выпуска. Замер на истории 24.09–07.10: два таких пропуска за две
# недели, но сырая выдача на ~95% мусор (соцсети, справочники, советы, старьё с
# новой датой изменения страницы). Отсюда фильтры: только новостные сайты, «Сбер»
# в заголовке, дата публикации ИЗ РАЗМЕТКИ СТРАНИЦЫ не старше SEARCH_MAX_AGE_H —
# дата выдачи Яндекса (modtime) пропустила бы статьи 2014 и 2019 годов.
SEARCH_SRC = {"key": "web_sber", "tag": "incident", "dimension": "fraud", "cls": "bank"}
SEARCH_EVERY_MIN = int(os.getenv("NEWSFLOW_SEARCH_EVERY_MIN", "120"))
SEARCH_MAX_AGE_H = int(os.getenv("NEWSFLOW_SEARCH_MAX_AGE_H", "48"))
SEARCH_MAX_FETCH = int(os.getenv("NEWSFLOW_SEARCH_MAX_FETCH", "60"))
SEARCH_PAGE = int(os.getenv("NEWSFLOW_SEARCH_PAGE", "50"))
# Разбор 11.11.2026 (пять недель работы): из 407 находок до сильной модели дошли
# 13, в выпуск — 3, и ни одной истории, которой не было бы в лентах. Причины:
# • у Яндекса брались первые 10 документов «по релевантности», а в двухдневном
#   окне свежести наверху стоит самое старое — находки приходили раз в сутки,
#   ночью, через 36–45 часов после публикации. Теперь берём до SEARCH_PAGE
#   документов в двух порядках: по релевантности и по времени;
# • запросы были только про хищения и уголовные дела. Проверка на живой выдаче
#   за неделю: прежние семь нашли 0 пропущенных историй, а запросы про списания,
#   надзор и суды — три («Сбербанк признал повторное списание средств со счетов»,
#   её продолжение и «Роспотребнадзор по Коми добрался до Сбербанка»); в лентах
#   не было ни одной.
SEARCH_QUERIES = (
    # хищения и уголовные дела
    "Сбербанк хищение",
    "у Сбербанка похитили",
    "сотрудник Сбербанка задержан",
    "сотрудник Сбербанка осуждён",
    "Сбербанк уголовное дело",
    "служба безопасности Сбербанка",
    "Сбербанк мошенническая схема",
    "Сбербанк обналичивание",
    # деньги и данные клиентов
    "клиентов Сбербанка похитил деньги со счетов",
    "Сбербанк ошибочное списание",
    "Сбербанк утечка данных клиентов",
    # надзор и суды
    "Сбербанк прокуратура клиент вернули деньги",
    "суд обязал Сбербанк вернуть",
    "Сбербанк оштрафован",
    "Сбербанк Роспотребнадзор",
)
# не новости: соцсети и видео, мессенджеры, справочники и судебные базы, сайты
# банков и агрегаторов (banki.ru и так в лентах), энциклопедии; вакансии, форумы
# и вопросы-ответы, торговые площадки, дочерние сайты Сбера
_SEARCH_SKIP = re.compile(
    r"(?:^|\.)(?:dzen\.ru|vk\.(?:ru|com)|vkvideo\.ru|rutube\.ru|youtube\.com|youtu\.be|t\.me|"
    r"max\.ru|ok\.ru|pikabu\.ru|otzovik\.com|irecommend\.ru|livejournal\.com|teletype\.in|"
    r"tiktok\.com|sudact\.ru|garant\.ru|harant\.ru|consultant\.ru|zakonrf\.info|sudrf\.ru|"
    r"kad\.arbitr\.ru|pravo\.gov\.ru|cyberleninka\.ru|wikipedia\.org|tadviser\.ru|"
    r"sberbank\.(?:ru|com)|sber\.ru|sbersova\.ru|banki\.ru|sravni\.ru|finuslugi\.ru|"
    r"bankiros\.ru|vbr\.ru|"
    r"hh\.ru|superjob\.ru|rabota\.ru|changellenge\.com|otvet\.mail\.ru|meshok\.net|avito\.ru|"
    r"sberleasing\.ru|sberbank-ast\.ru)$"
    r"|(?:^|\.)forum[a-z0-9-]*\.", re.I)
_SBER_RE = re.compile(r"сбер", re.I)
# Мусорные домены SEO-выдачи (на проде в первый же проход: meshlink.mom — «как снять деньги…»)
_JUNK_TLD = re.compile(r"\.(?:mom|site|xyz|online|top|click|shop|store|fun|space|icu|buzz|website|pw)$", re.I)
# Поиск отвечает только за ПРОИСШЕСТВИЯ: остальное с его выдачи — кликбейт «Сбербанк изменил
# правила для всех» и советы, которые ступень 1 оценивала на 6–9. Не происшествие из поиска →
# rel не выше 4: дальше по конвейеру не идёт (полный текст, склейка и ступень 2 — от 5–6).
SEARCH_TYPES = ("fraud", "enforcement", "court", "incident", "data_leak", "consumer", "regulator")
SEARCH_OTHER_REL_CAP = 4
# Потолок ценности происшествия, известного от одного сайта из поиска: в ленту
# выпуска проходит (от 6), в поводы дня — нет (от 7). См. unconfirmed_incident.
SEARCH_SOLO_CAP = 6
# Страницы «работает ли сегодня»: собираются автоматически каждый день
# («Сегодня, 6 ноября, у многих пользователей не работает Сбербанк»,
# «Сбербанк 26 октября работает с перебоями») — не новости.
_SEARCH_DAILY = re.compile(
    r"^\s*сегодня,?\s+\d{1,2}\s+[а-яё]+|сбер\w*\s+\d{1,2}\s+[а-яё]+\s+работает с перебоями", re.I)
_OG_TITLE_RE = re.compile(r'<meta[^>]{0,200}?property="og:title"[^>]{0,200}?content="([^"]{10,300})"'
                          r'|<meta[^>]{0,200}?content="([^"]{10,300})"[^>]{0,200}?property="og:title"', re.I)


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/:?#]+)", url or "", re.I)
    return (m.group(1).lower() if m else "").removeprefix("www.")


def _og_title(page: str) -> str:
    import html as _html
    m = _OG_TITLE_RE.search(page or "")
    return " ".join(_html.unescape(m.group(1) or m.group(2)).split()) if m else ""


_H1_RE = re.compile(r"<h1[^>]*>.*?</h1>", re.S | re.I)
_NOSCRIPT_RE = re.compile(r"<script.*?</script>|<style.*?</style>", re.S | re.I)
_D_RU_ANY = re.compile(r"(?<!\d)(\d{1,2})\s+(" + "|".join(_RU_MONTHS) + r")(?:\s+(20\d\d))?", re.I)


def page_date_fallback(page: str, url: str, hint: str | None,
                       now: datetime | None = None) -> datetime | None:
    """Дата публикации страницы, у которой её нет в разметке (так у большинства
    региональных изданий: 11.11.2026 из-за этого отброшено 13 кандидатов из 22).

    Берём число из адреса («/2026/11/09/») или стоящее рядом с заголовком — но
    только если оно сходится с датой документа у Яндекса (hint) с точностью до
    дня. Сама дата Яндекса — время обхода, а не публикации: у майской статьи она
    была ноябрьской; совпадение двух независимых дат такую статью отсекает."""
    try:
        hd = date.fromisoformat((hint or "")[:10])
    except ValueError:
        return None
    cands: list[date] = []
    m = nm._URL_DATE_RE.search(url or "")
    if m:
        try:
            cands.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    body = _NOSCRIPT_RE.sub(" ", page or "")
    h1 = _H1_RE.search(body)
    if h1:
        near = nm._strip_html(body[max(0, h1.start() - 1500): h1.end() + 2500])
        hits = [(x.start(), int(x.group(3)), int(x.group(2)), int(x.group(1))) for x in _D_NUM.finditer(near)]
        hits += [(x.start(), int(x.group(3) or hd.year), _RU_MONTHS.index(x.group(2).lower()) + 1, int(x.group(1)))
                 for x in _D_RU_ANY.finditer(near)]
        for _pos, y, mo, d in sorted(hits):
            try:
                cands.append(date(y, mo, d))
            except ValueError:
                continue
    hit = next((c for c in cands if abs((c - hd).days) <= 1), None)
    if not hit:
        return None
    now = now or datetime.now(timezone.utc)
    return min(datetime(hit.year, hit.month, hit.day, 12, tzinfo=MSK), now)


# Отброшенное (нет даты, старше окна) держится в выдаче двое суток: без памяти
# каждая такая страница скачивалась бы заново каждые два часа.
_SEARCH_REJECTED: dict[str, float] = {}
_SEARCH_REJECT_TTL_S = 48 * 3600


def _rejected_recently(url: str, now_s: float) -> bool:
    for u in [u for u, t in _SEARCH_REJECTED.items() if now_s - t > _SEARCH_REJECT_TTL_S]:
        _SEARCH_REJECTED.pop(u, None)
    return url in _SEARCH_REJECTED


def _search_due() -> bool:
    with db.session() as s:
        last = s.execute(text("SELECT last_ok_at FROM news_source_state WHERE source = :s"),
                         {"s": SEARCH_SRC["key"]}).scalar()
    return not last or datetime.now(timezone.utc) - last >= timedelta(minutes=SEARCH_EVERY_MIN)


def collect_search(force: bool = False) -> dict:
    """Поиск по теме «происшествия со Сбером» раз в SEARCH_EVERY_MIN минут.
    Найденное ложится в news_item как обычная запись (источник web_sber) и идёт
    тем же конвейером: ступень 1, полный текст, склейка, ступень 2."""
    from ..rag import search_gateway as sg
    if not sg.enabled():
        return {"skipped": "поиск не настроен"}
    if not force and not _search_due():
        return {"skipped": "рано"}
    seen: set[str] = set()
    cands: list[dict] = []
    err = None
    for q in SEARCH_QUERIES:
        # два порядка: по времени — чтобы свежее не ждало, пока состарится до
        # первых мест, по релевантности — полнота (у порядка по времени Яндекс
        # отдаёт меньше документов)
        for by_time in (True, False):
            r = sg.yandex_search(q, fresh_hours=SEARCH_MAX_AGE_H, max_results=SEARCH_PAGE,
                                 page_size=SEARCH_PAGE, by_time=by_time, caller="newsflow")
            if r.status not in ("ok", "empty"):
                err = f"поиск {r.status}: {(r.detail or '')[:120]}"
                continue
            for it in r.items:
                u = it.get("url") or ""
                if not u or u in seen:
                    continue
                seen.add(u)
                host = _host_of(u)
                title = it.get("title") or ""
                if _SEARCH_SKIP.search(host) or _JUNK_TLD.search(host) \
                        or not _SBER_RE.search(title) or _SEARCH_DAILY.search(title):
                    continue
                cands.append(it)
    if cands:                                   # уже виденные адреса не качаем
        with db.session() as s:
            have = {r[0] for r in s.execute(text(
                "SELECT url_hash FROM news_item WHERE url_hash = ANY(:h)"),
                {"h": [nm._url_hash(c["url"]) for c in cands]}).all()}
        cands = [c for c in cands if nm._url_hash(c["url"]) not in have]
    now = datetime.now(timezone.utc)
    cands = [c for c in cands if not _rejected_recently(c["url"], now.timestamp())]
    rows, dropped = [], {"no_date": 0, "old": 0, "http": 0}
    for c in cands[:SEARCH_MAX_FETCH]:
        try:
            r = nm._get(c["url"])
            if r.status_code != 200:
                dropped["http"] += 1
                continue
            page = r.text
            ts = nm.date_from_html(page) or page_date_fallback(page, c["url"], c.get("date"), now)
        except Exception:  # noqa: BLE001 — одна страница, не весь проход
            dropped["http"] += 1
            continue
        if not ts:
            dropped["no_date"] += 1
            _SEARCH_REJECTED[c["url"]] = now.timestamp()
            continue
        if ts < now - timedelta(hours=SEARCH_MAX_AGE_H) or ts > now + timedelta(hours=3):
            dropped["old"] += 1
            _SEARCH_REJECTED[c["url"]] = now.timestamp()
            continue
        title = _og_title(page) or clean_title(c.get("title") or "")
        if not _SBER_RE.search(title):
            title = clean_title(c.get("title") or "")
        rows.append(_row(SEARCH_SRC, c["url"], ts, title, c.get("snippet") or ""))
    n = 0
    if rows:
        with db.session() as s:
            res = s.execute(_INS, rows)
            n = res.rowcount if res.rowcount and res.rowcount > 0 else 0
    with db.session() as s:
        s.execute(_STATE_UPSERT, {"s": SEARCH_SRC["key"], "e": err, "lp": None})
    out = {"queries": len(SEARCH_QUERIES), "candidates": len(cands), "added": n, "dropped": dropped}
    log.info("newsflow.search: %s", out)
    return out


# ── Модель ───────────────────────────────────────────────────────────────────
async def _chat(model: str, system: str, user: str, max_tokens: int) -> tuple[str, int, int]:
    from .writer import _chat as wchat
    return await wchat(model, system, user, max_tokens=max_tokens, temperature=0.0)


def _loose(raw: str):
    from ..ai.llm_utils import _loose_json_loads
    return _loose_json_loads(raw)


_S1_TYPES = ("regulator", "law", "enforcement", "fraud", "incident", "data_leak", "sber",
             "court", "payments", "consumer", "competitor", "rates", "market", "macro",
             "corporate", "other")

_S1_SYSTEM = (
    "Ты — первый фильтр новостной ленты для службы внутреннего аудита РОЗНИЧНОГО бизнеса "
    "крупного российского банка. Для каждой позиции оцени rel 0–10 — насколько это может "
    "пригодиться аудитору розницы — и тип события.\n"
    "7–10: действия ЦБ, прокуратуры, судов, ФАС, Роспотребнадзора в отношении банков и их "
    "розничных практик; новые законы и нормативы по рознице, платежам, ПОД/ФТ; схемы "
    "мошенничества против клиентов банков; сбои, утечки, инциденты в банках; события Сбера; "
    "судебная практика по банковской рознице; защита прав потребителей финуслуг; статистика "
    "ЦБ по жалобам и мошенничеству; опубликованные законы и акты (указания ЦБ, приказы) о "
    "банках, платежах, кредитах и вкладах граждан, ПОД/ФТ, персональных данных, антифроде.\n"
    "5–6: рынок розничных банковских продуктов: ставки, тарифы, акции банков, ключевая "
    "ставка, объёмы кредитов и вкладов, продукты конкурентов.\n"
    "3–4: финансы и макроэкономика без связи с розницей: фондовый рынок, облигации, "
    "корпоративные новости, бюджет, курс.\n"
    "0–2: не про финансовый сектор РФ: политика, происшествия, спорт, наука, зарубежные "
    "новости без связи с РФ, реклама, подпись канала, рубрика без содержания; акты и "
    "новости ведомств не о финансах (госслужба, ЖКХ, здравоохранение, питание).\n"
    f"type — одно из: {', '.join(_S1_TYPES)}.\n"
    'Ответ строго JSON без markdown: {"items":[{"n":1,"rel":7,"type":"fraud"}]} — по всем позициям.'
)


async def stage1(limit: int = 600) -> dict:
    """Дешёвая модель по всем новым записям пакетами по 40."""
    with db.session() as s:
        rows = s.execute(text("""
            SELECT id, source, title, left(body, 400) FROM news_item
            WHERE s1_at IS NULL AND first_seen > now() - interval '72 hours'
            ORDER BY ts DESC NULLS LAST LIMIT :n"""), {"n": limit}).all()
    if not rows:
        return {"scored": 0}
    tin = tout = 0
    done = 0
    sem = asyncio.Semaphore(6)

    async def one(chunk):
        nonlocal tin, tout, done
        listing = "\n".join(f"#{k + 1} [{r[1]}] {r[2]} — {(r[3] or '')[:300]}"
                            for k, r in enumerate(chunk))
        async with sem:
            try:
                raw, a, b = await _chat(S1_MODEL, _S1_SYSTEM, listing, max_tokens=4000)
                d = _loose(raw) or {}
            except Exception as e:  # noqa: BLE001
                log.warning("newsflow.stage1: пакет не оценён (%s)", e)
                return
        tin += a
        tout += b
        items = [it for it in d.get("items") or [] if isinstance(it, dict)]
        by = {int(it["n"]): it for it in items if str(it.get("n", "")).isdigit()}
        if not by and len(items) == len(chunk):
            by = {k + 1: it for k, it in enumerate(items)}
        payload = []
        for k, r in enumerate(chunk):
            it = by.get(k + 1)
            if not it:
                continue
            try:
                rel = max(0, min(10, int(it.get("rel"))))
            except (TypeError, ValueError):
                continue
            typ = it.get("type") if it.get("type") in _S1_TYPES else "other"
            if r[1] == SEARCH_SRC["key"] and typ not in SEARCH_TYPES:
                rel = min(rel, SEARCH_OTHER_REL_CAP)
            payload.append({"id": r[0], "rel": rel, "t": typ})
        if payload:
            with db.session() as s:
                s.execute(text("UPDATE news_item SET rel = :rel, rtype = :t, s1_at = now() WHERE id = :id"),
                          payload)
            done += len(payload)

    await asyncio.gather(*(one(rows[j:j + 40]) for j in range(0, len(rows), 40)))
    return {"scored": done, "tokens": (tin, tout)}


def fetch_bodies(limit: int = 40) -> int:
    """Полный текст статей для записей, прошедших ступень 1 (у Telegram текст
    поста полный сразу). Без Playwright: HTTP и разбор HTML."""
    with db.session() as s:
        rows = s.execute(text("""
            SELECT id, url FROM news_item
            WHERE body_full = false AND rel >= :m AND url NOT LIKE 'https://t.me/%'
              AND parent_url IS NULL AND first_seen > now() - interval '72 hours'
            ORDER BY rel DESC, ts DESC LIMIT :n"""), {"m": S2_MIN_REL, "n": limit}).all()
    if not rows:
        return 0
    from .writer import _news_bodies
    bodies = _news_bodies([r[1] for r in rows], max_chars=BODY_CHARS, max_n=len(rows))
    payload = []
    for rid, url in rows:
        if url not in bodies:           # не пробовали скачать — «полный текст» не ставим
            continue
        b = (bodies.get(url) or "").strip()
        payload.append({"id": rid, "b": b[:BODY_CHARS] if len(b) > 200 else None, "cap": BODY_CHARS})
    if not payload:
        return 0
    with db.session() as s:
        # у публикации со страницы-списка в body лежит пояснение из карточки
        # (одна-две фразы о сути) — оставляем его первым абзацем перед полным
        # текстом: оно нужно полке исследований и полезно модели
        s.execute(text("""
            UPDATE news_item SET body = CASE
                    WHEN CAST(:b AS text) IS NULL THEN body
                    WHEN tag = 'research' AND body <> title AND length(body) BETWEEN 40 AND 400
                        THEN left(body || chr(10) || chr(10) || CAST(:b AS text), :cap)
                    ELSE CAST(:b AS text) END,
                body_full = true
            WHERE id = :id"""), payload)
    return sum(1 for p in payload if p["b"])


def cluster(limit: int = 300) -> int:
    """Склейка в события: запись, похожая на уже известную (косинус ≥ 0.86 за
    72 ч), становится частью её события. Иначе — новое событие."""
    with db.session() as s:
        rows = s.execute(text("""
            SELECT id, title, left(body, 300) FROM news_item
            WHERE event_id IS NULL AND rel >= 5 AND first_seen > now() - interval '72 hours'
            ORDER BY ts NULLS LAST LIMIT :n"""), {"n": limit}).all()
    if not rows:
        return 0
    from ..rag import embedder
    vecs = embedder.embed_batch([f"{r[1]}. {r[2] or ''}"[:800] for r in rows])
    n = 0
    for (rid, _t, _b), v in zip(rows, vecs):
        vs = "[" + ",".join(f"{x:.6f}" for x in v) + "]"
        with db.session() as s:
            s.execute(text("UPDATE news_item SET emb = CAST(:v AS vector) WHERE id = :id"),
                      {"v": vs, "id": rid})
            hit = s.execute(text("""
                SELECT event_id, 1 - (emb <=> CAST(:v AS vector)) AS sim FROM news_item
                WHERE emb IS NOT NULL AND event_id IS NOT NULL AND id <> :id
                  AND coalesce(ts, first_seen) > now() - interval '72 hours'
                ORDER BY emb <=> CAST(:v AS vector) LIMIT 1"""), {"v": vs, "id": rid}).first()
            ev = hit[0] if hit and float(hit[1]) >= EVENT_SIM else rid
            s.execute(text("UPDATE news_item SET event_id = :e WHERE id = :id"), {"e": ev, "id": rid})
        n += 1
    return n


# ── Ступень 2 ────────────────────────────────────────────────────────────────
S2_CATEGORIES = ("enforcement", "regulation", "fraud", "incident", "data_leak", "sber",
                 "court", "payments", "consumer", "competitor_risk", "market_background",
                 "macro", "other")

S2_SYSTEM = (
    "Ты — руководитель службы внутреннего аудита РОЗНИЧНОГО бизнеса Сбера. Каждое утро ты "
    "решаешь, что из новостей может натолкнуть твоих аудиторов на НОВУЮ ПРОВЕРКУ или "
    "скорректировать текущую: нарушения прав потребителей и мисселинг, мошенничество и "
    "утечки, сбои процессов, требования регулятора и их сроки, практика ЦБ и судов против "
    "банков, риски в продуктах и каналах Сбера.\n"
    "Шкала value 0–10:\n"
    "9–10 — прямой повод для проверки: действие ЦБ, суда или прокуратуры против банка за "
    "нарушение в рознице (есть практика, которую надо проверить у Сбера); новая схема "
    "мошенничества или утечка с механикой; сбой у банка; закон или норматив с датой "
    "вступления, меняющий розничные процессы; событие в Сбере с риском.\n"
    "6–8 — полезный контекст для выбора проверок: статистика ЦБ по жалобам и "
    "мошенничеству, тренды проблем розницы (просрочка, мисселинг), рискованные практики "
    "конкурентов, проекты требований без даты; новые процессы, продукты и каналы самого "
    "Сбера (пилоты, изменения обслуживания, работа с наличными) — это проверка новых "
    "контролей; внешние угрозы каналам и инфраструктуре Сбера (магазины приложений, санкции "
    "против платёжной инфраструктуры); раскрытые схемы хищений у клиентов банков.\n"
    "3–5 — рыночный фон: ставки и тарифы банков, прогнозы ключевой ставки, продуктовые "
    "запуски без риска, объёмы рынка. Это фон для казначейства и маркетинга, не повод.\n"
    "0–2 — не про розничный банковский бизнес, реклама, устаревшее (событие старше 2 суток).\n"
    "Опорные примеры: «Росфинмониторинг предлагает блокировать счета при частичном совпадении "
    "данных с перечнем» — 8; «ЦБ рассматривает полный возврат похищенного мошенниками» — 8; "
    "«ЦБ оштрафовал 4 банка за нарушения по кредитным историям» — 9; «ЦБ оштрафовал Сбер за "
    "передачу данных в БКИ» — 10; «Максимальная ставка по вкладам топ-10 банков — 13,01%» — 3; "
    "«Ключевая ставка пойдёт вниз весной 2027: мнение аналитика» — 3; «Сбербанк тестирует выдачу "
    "карт через банкоматы» — 7; «Google может ограничить установку российских приложений на "
    "Android» — 8; «Задержаны организаторы кол-центров с инвестиционной схемой хищения» — 7; "
    "«ЦБ определил порядок допуска криптобирж в реестр» — 4; интервью «Эксперт рассказала, кому "
    "банки одобряют кредиты», где сказано, что с 1 октября банкам снова сократят квоту на выдачу "
    "кредитов закредитованным, — 8, и headline про квоту, а не про интервью.\n"
    "Обзоры, дайджесты, интервью и советы экспертов оценивай по САМОЙ важной для аудита розницы "
    "новости внутри и пиши headline про неё. Норматив или решение регулятора с датой вступления в "
    "ближайшие месяцы — повод (7–9), даже если он упомянут между делом.\n"
    "category — одно из: enforcement (санкции и штрафы регулятора, прокуратура), regulation "
    "(законы, нормативы, проекты требований, разъяснения ЦБ), fraud (схемы мошенничества), "
    "incident (сбои, аварии), data_leak (утечки данных), sber (событие самого Сбера), court "
    "(суды и судебная практика), payments (платёжная инфраструктура: СБП, карты, НСПК, "
    "цифровой рубль), consumer (защита прав потребителей, жалобы, мисселинг), competitor_risk "
    "(рискованные практики конкурентов), market_background (ставки, тарифы, объёмы рынка), "
    "macro (макроэкономика), other.\n"
    "Пиши по-русски, факты — ТОЛЬКО из переданного текста, ничего не выдумывай."
)

def _codes_hint() -> str:
    from ..rag import review_codebook as cb
    return "; ".join(f"{k} — {v[1]}" for k, v in cb.ISSUES.items() if k not in ("other", "no_issue"))


S2_FIELDS = ('{"n":1,"value":0,"category":"...","headline":"до 90 знаков, суть события",'
             '"summary":"1 предложение фактов из текста","sber":"какой процесс или продукт Сбера '
             'затронут, до 15 слов","idea":"что проверить аудитору, до 25 слов (пусто при value<6)",'
             '"request":"что запросить для проверки, до 15 слов (пусто при value<6)",'
             '"deadline":"YYYY-MM-DD если в тексте есть срок вступления или исполнения, иначе пусто",'
             '"codes":["до 3 кодов проблем из списка ПРОБЛЕМЫ ЖАЛОБ, с которыми связано событие"],'
             '"stale":false}')


def _lead_key(x: dict, auth: dict) -> tuple:
    """Порядок записей события: сначала не обзоры, затем авторитетность
    источника, полнота текста и время."""
    return (bool(_DIGEST_TITLE.search(x.get("title") or "")), auth.get(x["source"], 5),
            -len(x.get("body") or ""), x.get("ts") or datetime.max.replace(tzinfo=timezone.utc))


def _event_rows(ids: list[int] | None = None, since_h: float = 30) -> list[dict]:
    """События окна: ведущая запись (самый длинный текст у самого авторитетного
    источника) и заголовки остальных источников события."""
    with db.session() as s:
        # Найденное поиском узнаём с опозданием (Яндекс индексирует региональные
        # издания до суток): его давность для окна — от момента находки, иначе
        # запись старше окна оценки уже в день находки и не оценивается никогда
        # (так было с 11 событиями из 11 за две недели до 11.11.2026).
        rows = s.execute(text(f"""
            SELECT id, event_id, source, url, title, body, ts, first_seen, rel, rtype, value, s2, image
            FROM news_item
            WHERE event_id IS NOT NULL AND rel >= 5
              AND greatest(coalesce(ts, first_seen),
                           CASE WHEN source = :sk THEN first_seen END)
                  > now() - make_interval(hours => :h)
              {"AND event_id = ANY(:ids)" if ids else ""}
        """), {"h": since_h, "sk": SEARCH_SRC["key"], **({"ids": ids} if ids else {})}).mappings().all()
    ev: dict[int, list[dict]] = {}
    for r in rows:
        ev.setdefault(r["event_id"], []).append(dict(r))
    out = []
    auth = getattr(nm, "_AUTHORITY", {})
    for eid, items in ev.items():
        items.sort(key=lambda x: _lead_key(x, auth))
        lead = items[0]
        lead_s2 = next((x for x in items if x["s2"]), None)
        out.append({"event_id": eid, "lead": lead, "items": items,
                    "n_sources": len({x["source"] for x in items}),
                    "rel": max(x["rel"] or 0 for x in items),
                    "rtype": lead["rtype"], "s2": (lead_s2 or {}).get("s2"),
                    "value": (lead_s2 or {}).get("value"),
                    "ts": max((x["ts"] for x in items if x["ts"]), default=None),
                    "fresh": fresh_ts(items)})
    return out


def fresh_ts(items: list[dict]) -> datetime | None:
    """От какого момента считать давность события для окна выпуска: время
    публикации, а у записей поиска — время, когда мы их нашли."""
    key = SEARCH_SRC["key"]
    return max((t for t in ((x.get("first_seen") if x.get("source") == key else None) or x.get("ts")
                            for x in items) if t), default=None)


def search_only(e: dict) -> bool:
    """Событие известно только из поиска: в лентах его нет."""
    key = SEARCH_SRC["key"]
    return bool(e.get("items")) and all(x.get("source") == key for x in e["items"])


def unconfirmed_incident(e: dict) -> bool:
    """Происшествие, о котором пишет один сайт из поиска и молчат все ленты.
    Настоящий сбой за час расходится по лентам, а первая ступень ставила 8–10
    и страницам «у многих не работает Сбербанк» одного сайта. Такое событие
    в выпуск идёт строкой ленты, но поводом дня (карточкой, заголовком) стать
    не может, пока о нём не напишет второе издание."""
    if (e.get("s2") or {}).get("category") != "incident" or not search_only(e):
        return False
    return len({_host_of(x.get("url") or "") for x in e["items"]}) < 2


def _valid_codes(v) -> list[str]:
    from ..rag import review_codebook as cb
    if isinstance(v, str):
        v = [v]
    return [c for c in (v or []) if isinstance(c, str) and c in cb.ISSUES
            and c not in ("other", "no_issue")][:3]


_DATE = re.compile(r"(?<![\d.,])(?:0?[1-9]|[12]\d|3[01])\.(?:0[1-9]|1[0-2])(?:\.\d{2,4})?"
                   r"(?![\d,]|\.\d|\s*%)")
_NUM = re.compile(r"\d[\d\u00a0\u202f ]*(?:[.,]\d+)?")


def _nums(text_: str) -> set[float]:
    out = set()
    for m in _NUM.finditer(text_ or ""):
        raw = re.sub(r"[\u00a0\u202f ]", "", m.group(0)).replace(",", ".")
        try:
            out.add(float(raw))
        except ValueError:
            pass
    return out


def ungrounded_numbers(generated: str, source: str) -> list[str]:
    """Числа из текста модели, которых нет в источнике.

    Мелкие целые (до 10) не проверяются: «три банка» модель пишет цифрой, а
    в тексте словом. Годы тоже. Разрядность прощается: «600 тыс.» против
    «600 000», «1,2 млрд» против «1 200 млн»."""
    have = _nums(source)
    scaled = {round(v * k, 6) for v in have for k in (1, 1e3, 1e6, 1e9, 1e-3, 1e-6, 1e-9)}
    # даты модель переписывает в свой формат («01.07.2027» при «1 июля 2027»)
    generated = _DATE.sub(" ", generated or "")
    bad = []
    for m in _NUM.finditer(generated):
        raw = re.sub(r"[\u00a0\u202f ]", "", m.group(0)).replace(",", ".")
        try:
            v = float(raw)
        except ValueError:
            continue
        if (v <= 10 and v == int(v)) or 1900 <= v <= 2100:
            continue
        if round(v, 6) not in scaled:
            bad.append(m.group(0).strip())
    return bad


def _ground(s2: dict, e: dict) -> dict:
    """Факты — только из источника: заголовок с чужим числом заменяется
    исходным, предложение изложения с чужим числом выбрасывается."""
    src = " ".join([e["lead"]["title"] or "", e["lead"]["body"] or ""]
                   + [x.get("title") or "" for x in e["items"]])
    bad_h = ungrounded_numbers(s2["headline"], src)
    if bad_h:
        s2["headline"] = e["lead"]["title"]
    sents = re.split(r"(?<=[.!?])\s+", s2["summary"])
    keep = [x for x in sents if not ungrounded_numbers(x, src)]
    if len(keep) < len(sents):
        s2["summary"] = " ".join(keep)
    if bad_h or len(keep) < len(sents):
        s2["ungrounded"] = bad_h + [n for x in sents for n in ungrounded_numbers(x, src)]
    return s2


async def stage2(since_h: float = 30, limit: int = 120) -> dict:
    """Сильная модель по событиям окна без оценки: полный текст ведущей записи
    и заголовки других источников. Пакеты по 6 событий."""
    with db.session() as s:
        today_n = int(s.execute(text(
            "SELECT count(*) FROM news_item WHERE s2_at > date_trunc('day', now())")).scalar() or 0)
    room = max(0, S2_DAILY_CAP - today_n)
    evs = [e for e in _event_rows(since_h=since_h) if e["s2"] is None and e["rel"] >= S2_MIN_REL]
    evs.sort(key=lambda e: (-e["rel"], -(e["n_sources"])))
    evs = evs[:min(limit, room)]
    if not evs:
        return {"scored": 0}
    tin = tout = 0
    done = 0
    sem = asyncio.Semaphore(4)
    today = datetime.now(MSK).date().isoformat()

    async def one(chunk):
        nonlocal tin, tout, done
        parts = []
        for k, e in enumerate(chunk, 1):
            lead = e["lead"]
            others = [x for x in e["items"] if x is not lead][:5]
            ts = lead["ts"].astimezone(MSK).strftime("%d.%m %H:%M") if lead["ts"] else "?"
            found = (" Найдено поиском по региональным изданиям: такие сообщения доходят с "
                     "опозданием, давность до трёх суток устаревшей не считай."
                     if lead["source"] == SEARCH_SRC["key"] else "")
            parts.append(
                f"[{k}] Источник: {lead['source']}, {ts}.{found} Заголовок: {lead['title']}\n"
                f"Текст: {(lead['body'] or '')[:3500]}"
                + (f"\nТо же событие в других источниках ({e['n_sources']}): "
                   + "; ".join((x['title'] or '')[:120] for x in others) if others else ""))
        user = (f"Сегодня {today}. ПРОБЛЕМЫ ЖАЛОБ (коды): {_codes_hint()}\n\nСобытия:\n\n"
                + "\n\n".join(parts)
                + '\n\nВерни JSON: {"items":[' + S2_FIELDS + ", ...]} — по каждому событию.")
        async with sem:
            try:
                raw, a, b = await _chat(S2_MODEL, S2_SYSTEM, user, max_tokens=5000)
                d = _loose(raw) or {}
            except Exception as ex:  # noqa: BLE001
                log.warning("newsflow.stage2: пакет не оценён (%s)", ex)
                return
        tin += a
        tout += b
        items = [it for it in d.get("items") or [] if isinstance(it, dict)]
        by = {int(it["n"]): it for it in items if str(it.get("n", "")).isdigit()}
        if not by and len(items) == len(chunk):
            by = {k + 1: it for k, it in enumerate(items)}
        payload = []
        for k, e in enumerate(chunk, 1):
            it = by.get(k)
            if not it:
                continue
            try:
                val = max(0, min(10, int(it.get("value"))))
            except (TypeError, ValueError):
                continue
            if it.get("stale") in (True, "true"):
                val = min(val, 2)
            cat = it.get("category") if it.get("category") in S2_CATEGORIES else "other"
            hl = clean_line(str(it.get("headline") or ""))[:160]
            s2 = {"category": cat, "headline": hl if title_ok(hl) else e["lead"]["title"],
                  "summary": str(it.get("summary") or "")[:400],
                  "sber": str(it.get("sber") or "")[:200],
                  "idea": str(it.get("idea") or "")[:300] if val >= 6 else "",
                  "request": str(it.get("request") or "")[:200] if val >= 6 else "",
                  "deadline": (str(it.get("deadline") or "")[:10]
                               if re.match(r"20\d\d-\d\d-\d\d", str(it.get("deadline") or "")) else ""),
                  "codes": _valid_codes(it.get("codes")),
                  "n_sources": e["n_sources"], "model": S2_MODEL}
            s2 = _ground(s2, e)
            payload.append({"id": e["lead"]["id"], "v": val, "s2": json.dumps(s2, ensure_ascii=False)})
        if payload:
            with db.session() as s:
                s.execute(text("""UPDATE news_item SET value = :v, s2 = CAST(:s2 AS jsonb),
                                  s2_at = now() WHERE id = :id"""), payload)
            done += len(payload)

    await asyncio.gather(*(one(evs[j:j + 6]) for j in range(0, len(evs), 6)))
    log.info("newsflow.stage2: оценено событий %d (%d/%d токенов)", done, tin, tout)
    return {"scored": done, "tokens": (tin, tout)}


_TICK_LOCK: asyncio.Lock | None = None


async def tick() -> dict:
    """Один цикл: сбор → ступень 1 → полный текст → склейка → ступень 2.
    Фоновый цикл и утренний выпуск зовут его оба — замок не даёт оценить одни
    и те же события дважды (и заплатить за это дважды)."""
    global _TICK_LOCK
    if _TICK_LOCK is None:
        _TICK_LOCK = asyncio.Lock()
    async with _TICK_LOCK:
        return await _tick()


async def _tick() -> dict:
    out = {"collect": await asyncio.to_thread(collect)}
    try:
        out["search"] = await asyncio.to_thread(collect_search)
    except Exception as e:  # noqa: BLE001 — поиск не должен останавливать ленты
        log.warning("newsflow.search: %s", e)
    out["stage1"] = await stage1()
    out["bodies"] = await asyncio.to_thread(fetch_bodies)
    out["cluster"] = await asyncio.to_thread(cluster)
    out["stage2"] = await stage2()
    return out


# ── Выборка для выпуска ──────────────────────────────────────────────────────
_GROUP_OF = {"sber": "sber", "enforcement": "regulatory", "regulation": "regulatory",
             "court": "regulatory", "consumer": "regulatory", "fraud": "incidents",
             "incident": "incidents", "data_leak": "incidents", "payments": "incidents",
             "competitor_risk": "market", "market_background": "market", "macro": "market",
             "other": "other"}


# Норматив с близкой датой вступления на границе отбора (5 из 10) — в выпуск.
# 25–28.09 «с 1 октября банкам снова сократят квоту на выдачу кредитов
# закредитованным» четыре дня лежало с оценкой 5 внутри интервью эксперта;
# судья выпуска дважды отметил её как пропущенную (оценил на 8).
DATED_FLOOR_DAYS = int(os.getenv("NEWSFLOW_DATED_FLOOR_DAYS", "31"))


def _dated_floor(e: dict, today: date) -> None:
    s2 = e.get("s2") or {}
    if (e.get("value") or 0) != 5 or s2.get("category") not in ("regulation", "enforcement"):
        return
    try:
        left = (date.fromisoformat(s2.get("deadline") or "") - today).days
    except ValueError:
        return
    if 0 <= left <= DATED_FLOOR_DAYS:
        e["value"] = 6
        e["floor"] = "dated_regulation"


def day_events(window_h: float = 26, reg_window_h: float = 96,
               republish_from: date | None = None) -> list[dict]:
    """Оценённые события для выпуска: окно суток (регуляторика — 4 дня: решение
    пятницы доживает до понедельника), ещё не опубликованные в прошлые дни.

    republish_from — с какого дня публикации снова допускаются: в выпуске после
    выходных это суббота (выпуски выходных почти никто не читает), в обычный
    день — сегодня (повторная сборка того же дня)."""
    today = datetime.now(MSK).date()
    republish_from = republish_from or today
    # целые часы: окно в SQL — make_interval(hours => int)
    evs = _event_rows(since_h=int(max(window_h, reg_window_h)) + 1)
    out = []
    for e in evs:
        if e["s2"] is None:
            continue
        cat = (e["s2"] or {}).get("category")
        born = e.get("fresh") or e["ts"]
        age_h = ((datetime.now(timezone.utc) - born).total_seconds() / 3600.0) if born else 0
        limit_h = max(reg_window_h, window_h) if cat in ("enforcement", "regulation", "court") else window_h
        if age_h > limit_h:
            continue
        if unconfirmed_incident(e) and (e["value"] or 0) > SEARCH_SOLO_CAP:
            e["value"] = SEARCH_SOLO_CAP
        with db.session() as s:
            prev = s.execute(text("""SELECT min(published_on) FROM news_item
                                     WHERE event_id = :e AND published_on IS NOT NULL"""),
                             {"e": e["event_id"]}).scalar()
        if prev and prev < republish_from:
            continue
        _dated_floor(e, today)
        e["group"] = _GROUP_OF.get(cat, "other")
        out.append(e)
    out.sort(key=lambda e: (-(e["value"] or 0), -e["n_sources"]))
    return out


# ── Окно выпуска: после выходных — за выходные ───────────────────────────────
# В субботу и воскресенье «Обзор» открывают 1–2 человека, в будни 6–12 (замер
# 14–28.09). Понедельничный выпуск был выпуском за одно воскресенье — 28.09 в
# нём было 4 новости, а 26 новостей выпусков субботы и воскресенья почти никто
# не видел. Выпуск первого рабочего дня после нерабочих собирается с утра
# последнего рабочего дня, и новости выпусков выходных в него снова допускаются.
HOLIDAYS = {date.fromisoformat(x.strip()) for x in os.getenv("DIGEST_HOLIDAYS", "").split(",")
            if x.strip()}
NEWS_MAX = int(os.getenv("DIGEST_NEWS_MAX", "14"))
NEWS_MAX_AFTER_OFF = int(os.getenv("DIGEST_NEWS_MAX_AFTER_OFF", "24"))


def is_workday(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def issue_scope(day: date, now: datetime | None = None) -> dict:
    """Что охватывает выпуск дня: окна отбора, с какого дня можно повторять
    вышедшее, сколько новостей показывать и подпись для страницы."""
    now = now or datetime.now(MSK)
    off: list[date] = []
    prev = day - timedelta(days=1)
    while not is_workday(prev) and len(off) < 14:
        off.append(prev)
        prev -= timedelta(days=1)
    if not is_workday(day) or not off:
        return {"after_off": False, "window_h": 26.0, "reg_window_h": 96.0,
                "republish_from": day, "news_max": NEWS_MAX}
    gen_h = int(os.getenv("DIGEST_GEN_HOUR_MSK", "7"))
    # с запасом в 2 часа до утреннего выпуска последнего рабочего дня: что
    # тогда вышло, отсекает дата публикации, а оценённое позже — не теряется
    since = datetime(prev.year, prev.month, prev.day, gen_h, tzinfo=MSK) - timedelta(hours=2)
    window_h = max(26.0, (now - since).total_seconds() / 3600.0 + 1)
    return {"after_off": True, "window_h": window_h, "reg_window_h": max(96.0, window_h),
            "republish_from": min(off), "news_max": NEWS_MAX_AFTER_OFF,
            "since": since.isoformat(), "days_off": [d.isoformat() for d in sorted(off)],
            "prev_workday": prev.isoformat()}


_MERGE_AUTO = float(os.getenv("NEWSFLOW_MERGE_AUTO", "0.80"))
_MERGE_ASK = float(os.getenv("NEWSFLOW_MERGE_ASK", "0.66"))


async def merge_events(evs: list[dict]) -> list[dict]:
    """Одно событие из разных источников — одна позиция выпуска.

    Векторы полных текстов для этого не годятся: замер 24.09 — «Добрая воля» у
    Frank Media, Ведомостей и banki.ru давала сходство 0,5. Сравниваем
    заголовки, которые сильная модель уже переписала в едином стиле: одно
    событие даёт 0,81–0,98, разные — до 0,72. Выше 0,80 — склеиваем, серую
    зону 0,66–0,80 решает дешёвая модель вопросом «одно ли это событие»."""
    if len(evs) < 2:
        return evs
    from ..rag import embedder
    heads = [((e["s2"] or {}).get("headline") or e["lead"]["title"] or "")[:200] for e in evs]
    vecs = await asyncio.to_thread(embedder.embed_batch, heads)
    parent = list(range(len(evs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    ask = []
    for i in range(len(evs)):
        for j in range(i + 1, len(evs)):
            sim = embedder.cosine_similarity(vecs[i], vecs[j])
            if sim >= _MERGE_AUTO:
                parent[find(j)] = find(i)
            elif sim >= _MERGE_ASK:
                ask.append((i, j))
    # Пачки по 25 пар, запас по токенам и один повтор: 25.09 модель «думала» и
    # обрывала ответ на 60 парах при лимите 1500 — серая зона не склеилась, и
    # одна схема мошенничества вышла карточкой и строкой ленты.
    for start in range(0, min(len(ask), 100), 25):
        chunk = ask[start:start + 25]
        listing = "\n".join(f"#{k + 1}: «{heads[i]}» || «{heads[j]}»" for k, (i, j) in enumerate(chunk))
        for attempt in range(2):
            try:
                raw, _a, _b = await _chat(S1_MODEL,
                                          "Для каждой пары заголовков новостей ответь, об одном ли и том же "
                                          "конкретном событии они (same) или о разных (diff). Одна тема — "
                                          "ещё не одно событие. Если один заголовок — сводка нескольких "
                                          "новостей и одна из них — событие из другого заголовка, это same. "
                                          'Ответ JSON: {"items":[{"n":1,"v":"same"}]}',
                                          listing, max_tokens=6000)
                d = _loose(raw) or {}
                items = d.get("items") or []
                if not items:
                    raise ValueError("пустой ответ")
                for it in items:
                    k = int(it.get("n", 0)) - 1
                    if 0 <= k < len(chunk) and str(it.get("v")).lower() == "same":
                        i, j = chunk[k]
                        parent[find(j)] = find(i)
                break
            except Exception as ex:  # noqa: BLE001 — без проверки серой зоны просто не склеиваем
                if attempt:
                    log.warning("newsflow.merge: серая зона не проверена (%s)", ex)
    groups: dict[int, list[int]] = {}
    for i in range(len(evs)):
        groups.setdefault(find(i), []).append(i)
    out = []
    for idx in groups.values():
        members = sorted((evs[i] for i in idx), key=lambda e: -(e["value"] or 0))
        lead = dict(members[0])
        lead["merged_ids"] = [m["event_id"] for m in members]
        lead["n_sources"] = len({x["source"] for m in members for x in m["items"]})
        lead["items"] = [x for m in members for x in m["items"]]
        out.append(lead)
    out.sort(key=lambda e: (-(e["value"] or 0), -e["n_sources"]))
    return out


# ── Повторы и сюжеты ─────────────────────────────────────────────────────────
# 27 и 28.09 выпуск открывался одним и тем же — «с 1 октября наличные через
# чужой банкомат по СБП»: вчера статья banki.ru, сегодня РИА. Новая статья
# стала новым событием (склейка по тексту для одного сюжета из разных изданий
# даёт ~0,5), а «уже публиковалось» проверялось по номеру события. Теперь
# кандидаты выпуска сверяются с вышедшим за 14 дней по заголовкам в едином
# стиле (s2.headline): близкие пары решает дешёвая модель — то же событие,
# продолжение сюжета или другое, и есть ли существенный новый факт.
# То же событие без нового факта — повтор, в выпуск не идёт; с новым фактом
# или новым развитием — продолжение сюжета: выходит с пометкой и не открывает
# выпуск.
REPEAT_AUTO = float(os.getenv("NEWSFLOW_REPEAT_AUTO", "0.80"))
REPEAT_ASK = float(os.getenv("NEWSFLOW_REPEAT_ASK", "0.66"))
STORY_DAYS = int(os.getenv("NEWSFLOW_STORY_DAYS", "14"))

_REPEAT_SYSTEM = (
    "Ты сверяешь новости сегодняшнего выпуска с уже вышедшими. Для каждой пары «БЫЛО» и "
    "«СЕЙЧАС» ответь rel: same — то же самое событие (тот же закон, решение, схема, "
    "инцидент, запуск); story — новое развитие того же сюжета (решение по делу, реакция "
    "регулятора или банков, вступление в силу, новые данные по тому же случаю); diff — "
    "разные события (общая тема — ещё не сюжет). new — существенный новый факт в «СЕЙЧАС», "
    "которого нет в «БЫЛО», одной фразой до 20 слов: новое решение, дата, сумма, масштаб, "
    "реакция. Пересказ другими словами и подробности того же — не новое, тогда new пустой. "
    'Ответ JSON: {"items":[{"n":1,"rel":"same","new":""}]}'
)


def _head(e: dict) -> str:
    return ((e.get("s2") or {}).get("headline") or e["lead"]["title"] or "")[:200]


def published_episodes(before: date, days: int = STORY_DAYS) -> list[dict]:
    """Вышедшие в выпусках события до дня before: дата выхода, заголовок в
    едином стиле и суть (с ведущей записи, у которой есть оценка)."""
    with db.session() as s:
        rows = s.execute(text("""
            SELECT DISTINCT ON (event_id) event_id, published_on,
                   coalesce(s2->>'headline', title) AS head,
                   coalesce(s2->>'summary', '') AS summary, url
            FROM news_item
            WHERE published_on IS NOT NULL AND event_id IS NOT NULL
              AND published_on < :b AND published_on >= :f
            ORDER BY event_id, (s2 IS NULL), published_on, id
        """), {"b": before, "f": before - timedelta(days=days)}).mappings().all()
    return [dict(r) for r in rows]


async def classify_repeats(cands: list[dict], before: date) -> dict[int, dict]:
    """event_id кандидата → {"kind": "repeat"|"continuation", "new_fact", "prev",
    "episodes"}. Кандидаты без пары в прошлых выпусках в ответ не попадают.
    Модель недоступна — близкие (≥ REPEAT_AUTO) считаются продолжением, а не
    повтором: лучше показать с пометкой, чем потерять новость."""
    if not cands:
        return {}
    prev = [p for p in await asyncio.to_thread(published_episodes, before) if title_ok(p["head"])]
    if not prev:
        return {}
    from ..rag import embedder
    vc = await asyncio.to_thread(embedder.embed_batch, [_head(e) for e in cands])
    vp = await asyncio.to_thread(embedder.embed_batch, [p["head"] for p in prev])
    sims = [[embedder.cosine_similarity(a, b) for b in vp] for a in vc]
    pairs = []
    for i, row in enumerate(sims):
        j = max(range(len(prev)), key=row.__getitem__)
        if row[j] >= REPEAT_ASK:
            pairs.append((i, j, row[j]))
    verdict: dict[int, tuple[str, str]] = {}
    for start in range(0, len(pairs), 20):
        chunk = pairs[start:start + 20]
        listing = "\n\n".join(
            f"#{k + 1}\nБЫЛО ({prev[j]['published_on']:%d.%m}): «{prev[j]['head']}» — "
            f"{(prev[j]['summary'] or '')[:300]}\n"
            f"СЕЙЧАС: «{_head(cands[i])}» — {((cands[i].get('s2') or {}).get('summary') or '')[:300]}"
            for k, (i, j, _sim) in enumerate(chunk))
        for attempt in range(2):
            try:
                raw, _a, _b = await _chat(S1_MODEL, _REPEAT_SYSTEM, listing, max_tokens=4000)
                items = (_loose(raw) or {}).get("items") or []
                if not items:
                    raise ValueError("пустой ответ")
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    k = int(it.get("n", 0)) - 1
                    if 0 <= k < len(chunk):
                        verdict[chunk[k][0]] = (str(it.get("rel") or "").strip().lower(),
                                                clean_line(str(it.get("new") or ""))[:200])
                break
            except Exception as ex:  # noqa: BLE001 — без проверки: близкие — продолжением
                if attempt:
                    log.warning("newsflow.repeats: пары не проверены (%s)", ex)
    out: dict[int, dict] = {}
    for i, j, sim in pairs:
        rel, new = verdict.get(i, ("", ""))
        if not rel:
            if sim < REPEAT_AUTO:
                continue
            rel = "story"
        if rel not in ("same", "story"):
            continue
        eps = {(p["published_on"], p["head"], p["url"]) for jj, p in enumerate(prev)
               if jj == j or sims[i][jj] >= REPEAT_AUTO}
        out[cands[i]["event_id"]] = {
            "kind": "repeat" if rel == "same" and not new else "continuation",
            "new_fact": new, "sim": round(sim, 3),
            "prev": {"title": prev[j]["head"][:160], "url": prev[j]["url"],
                     "date": prev[j]["published_on"].isoformat()},
            "episodes": [{"title": h[:140], "url": u, "date": d.isoformat()}
                         for d, h, u in sorted(eps)[-3:]]}
    if out:
        log.info("newsflow.repeats: повторов %d, продолжений %d",
                 sum(1 for v in out.values() if v["kind"] == "repeat"),
                 sum(1 for v in out.values() if v["kind"] == "continuation"))
    return out


def mark_published(event_ids: list[int]) -> None:
    if not event_ids:
        return
    with db.session() as s:
        s.execute(text("""UPDATE news_item SET published_on = (now() AT TIME ZONE 'Europe/Moscow')::date
                          WHERE event_id = ANY(:e) AND published_on IS NULL"""), {"e": event_ids})


# ── Полка «Исследования и обзоры» ────────────────────────────────────────────
# Исследование — не новость дня: в выпуск оно попадает, только если сильная
# модель увидела в нём повод для проверки. Чтобы новые источники из списка
# руководства были видны и без этого, свежие публикации по теме финансов
# показываются отдельной полкой под карточками выпуска.
# от 5 по шкале ступени 1 — про банки и розничные продукты; на 4 проходили
# анонсы выступлений на конференциях и общая макроэкономика (замер 11.11.2026)
SHELF_MIN_REL = int(os.getenv("NEWSFLOW_SHELF_MIN_REL", "5"))
SHELF_PER_SOURCE = int(os.getenv("NEWSFLOW_SHELF_PER_SOURCE", "3"))
SHELF_NEW_DAYS = 7


def research_shelf(limit: int = 8) -> list[dict]:
    """Последние публикации источников-исследований: не больше SHELF_PER_SOURCE
    от одного источника, без материалов не по теме (оценка ступени 1 ниже
    SHELF_MIN_REL). Неоценённое (только что собрано) показываем."""
    keys = list(nm.RESEARCH_KEYS)
    if not keys:
        return []
    with db.session() as s:
        # при равной дате — в порядке страницы (записи вставлены сверху вниз,
        # верхняя на сайте — самая свежая);
        # по нескольку самых свежих от каждого источника — иначе сотня докладов
        # одного сайта вытеснила бы остальные из выборки; с запасом, потому что
        # повторы одной серии полка пропускает (shelf_pick)
        rows = s.execute(text("""
            SELECT id, source, url, title, ts, first_seen, rel, value, summary, body, body_full FROM (
                SELECT id, source, url, title, ts, first_seen, rel, value,
                       s2->>'summary' AS summary, left(body, 400) AS body, body_full,
                       row_number() OVER (PARTITION BY source ORDER BY ts DESC NULLS LAST, id) AS rn
                FROM news_item
                -- у источника «обо всех рынках» (cls=gen) планка на балл выше: на 5
                -- прошли продажи лакокрасочных материалов
                WHERE source = ANY(:k)
                  AND (rel IS NULL OR rel >= CASE WHEN cls = 'gen' THEN :m + 1 ELSE :m END)) q
            WHERE rn <= :p
            ORDER BY ts DESC NULLS LAST, id
        """), {"k": keys, "m": SHELF_MIN_REL, "p": SHELF_PER_SOURCE * 4}).mappings().all()
    return shelf_pick([dict(r) for r in rows], limit)


def shelf_pick(rows: list[dict], limit: int = 8, now: datetime | None = None) -> list[dict]:
    """Строки полки из записей, уже упорядоченных от новых к старым."""
    now = now or datetime.now(timezone.utc)
    per: dict[str, int] = {}
    series: set[tuple[str, str]] = set()
    out = []
    for r in rows:
        if per.get(r["source"], 0) >= SHELF_PER_SOURCE:
            continue
        # ежемесячные записки одной серии («Кредитование субъектов МСП. Июнь…»,
        # «…Июль…», «…Август…») — на полке только свежая
        stem = (r["source"], re.sub(r"[^a-zа-яё ]+", "", (r["title"] or "").lower())[:24].strip())
        if stem in series:
            continue
        series.add(stem)
        per[r["source"]] = per.get(r["source"], 0) + 1
        ts, seen = r["ts"], r["first_seen"]
        # дата известна, если она взята со страницы (полдень по Москве) или
        # публикацию нашли сразу; у лежавшего на сайте до нас даты нет
        dated = bool(ts) and ((ts.astimezone(MSK).strftime("%H:%M:%S") == "12:00:00")
                              or (seen is not None and abs((seen - ts).total_seconds()) < 36 * 3600))
        note = (r["summary"] or "").strip()
        if not note:
            raw = r["body"] or ""
            if r.get("body_full"):      # пояснение из карточки — первый абзац перед полным текстом
                raw = raw.split("\n\n", 1)[0] if "\n\n" in raw else ""
            b = clean_line(raw)
            note = b if letters(b) >= 40 and b[:40].lower() != (r["title"] or "")[:40].lower() else ""
        out.append({"id": r["id"], "source": r["source"], "url": r["url"], "title": r["title"],
                    "date": ts.astimezone(MSK).date().isoformat() if dated else None,
                    "new": bool(dated and (now - ts).days < SHELF_NEW_DAYS),
                    "note": note[:300], "value": r["value"],
                    "pdf": (r["url"] or "").lower().split("?")[0].endswith(".pdf")})
        if len(out) >= limit:
            break
    return out


def health() -> dict:
    """Для «Пульса»: сколько собрано за сутки по источникам, ошибки, отбор."""
    with db.session() as s:
        src = [dict(r) for r in s.execute(text("""
            SELECT source, last_ok_at, last_error, items_24h FROM news_source_state ORDER BY source
        """)).mappings().all()]
        agg = s.execute(text("""
            SELECT count(*), count(*) FILTER (WHERE rel >= 6), count(*) FILTER (WHERE value >= 7),
                   count(DISTINCT event_id) FILTER (WHERE rel >= 5)
            FROM news_item WHERE coalesce(ts, first_seen) > now() - interval '24 hours'
        """)).one()
        # Отдача источника за 2 недели: слабые исключаем по цифрам, а не на глаз.
        # «Сильный» — материал события с ценностью от 7 (оценка стоит на ведущей
        # записи события, поэтому берём максимум по событию).
        yld = [dict(r) for r in s.execute(text("""
            WITH ev AS (
                SELECT event_id, max(value) AS v, bool_or(published_on IS NOT NULL) AS pub
                FROM news_item
                WHERE event_id IS NOT NULL AND first_seen > now() - interval '14 days'
                GROUP BY 1)
            SELECT n.source, count(*) AS items,
                   count(*) FILTER (WHERE n.rel >= 6) AS relevant,
                   count(*) FILTER (WHERE ev.v >= 7) AS strong,
                   count(*) FILTER (WHERE ev.pub) AS published
            FROM news_item n LEFT JOIN ev USING (event_id)
            WHERE n.first_seen > now() - interval '14 days'
            GROUP BY 1 ORDER BY 4 DESC, 3 DESC
        """)).mappings().all()]
        # Поиск по теме окупается событиями, которых в лентах НЕ было вовсе:
        # событие из одних записей web_sber. Через две недели решаем по ним.
        sch = s.execute(text("""
            WITH ev AS (
                SELECT event_id, bool_and(source = :k) AS only_search, bool_or(source = :k) AS any_search,
                       max(value) AS v, bool_or(published_on IS NOT NULL) AS pub
                  FROM news_item
                 WHERE event_id IS NOT NULL AND first_seen > now() - interval '14 days'
                 GROUP BY 1)
            SELECT count(*) FILTER (WHERE any_search), count(*) FILTER (WHERE only_search),
                   count(*) FILTER (WHERE only_search AND v >= 6),
                   count(*) FILTER (WHERE only_search AND pub)
              FROM ev"""), {"k": SEARCH_SRC["key"]}).one()
    return {"sources": src, "items_24h": int(agg[0]), "relevant_24h": int(agg[1]),
            "strong_24h": int(agg[2]), "events_24h": int(agg[3]), "yield_14d": yld,
            "search_14d": {"events": int(sch[0]), "only_search": int(sch[1]),
                           "only_search_strong": int(sch[2]), "only_search_published": int(sch[3])}}
