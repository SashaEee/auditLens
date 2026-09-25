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
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

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
            s.execute(text("""
                INSERT INTO news_source_state (source, last_ok_at, last_error, last_post, items_24h)
                VALUES (:s, CASE WHEN CAST(:e AS text) IS NULL THEN now() END, CAST(:e AS text),
                        CAST(:lp AS bigint),
                        (SELECT count(*) FROM news_item WHERE source = :s AND ts > now() - interval '24 hours'))
                ON CONFLICT (source) DO UPDATE SET
                    last_ok_at = coalesce(EXCLUDED.last_ok_at, news_source_state.last_ok_at),
                    last_error = EXCLUDED.last_error,
                    last_post = coalesce(EXCLUDED.last_post, news_source_state.last_post),
                    items_24h = EXCLUDED.items_24h
            """), {"s": src["key"], "e": err, "lp": last_post})
    return {"added": added, "sources": per}


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
    import os as _os
    _os.environ.setdefault("DIGEST_NEWS_BODY_CHARS", str(BODY_CHARS))
    bodies = _news_bodies([r[1] for r in rows])
    payload = []
    for rid, url in rows:
        b = (bodies.get(url) or "").strip()
        payload.append({"id": rid, "b": b[:BODY_CHARS] if len(b) > 200 else None})
    with db.session() as s:
        s.execute(text("""UPDATE news_item SET body = coalesce(:b, body), body_full = true
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
    "«ЦБ определил порядок допуска криптобирж в реестр» — 4.\n"
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


def _event_rows(ids: list[int] | None = None, since_h: float = 30) -> list[dict]:
    """События окна: ведущая запись (самый длинный текст у самого авторитетного
    источника) и заголовки остальных источников события."""
    with db.session() as s:
        rows = s.execute(text(f"""
            SELECT id, event_id, source, url, title, body, ts, rel, rtype, value, s2, image
            FROM news_item
            WHERE event_id IS NOT NULL AND rel >= 5
              AND coalesce(ts, first_seen) > now() - make_interval(hours => :h)
              {"AND event_id = ANY(:ids)" if ids else ""}
        """), {"h": since_h, **({"ids": ids} if ids else {})}).mappings().all()
    ev: dict[int, list[dict]] = {}
    for r in rows:
        ev.setdefault(r["event_id"], []).append(dict(r))
    out = []
    auth = getattr(nm, "_AUTHORITY", {})
    for eid, items in ev.items():
        items.sort(key=lambda x: (auth.get(x["source"], 5), -len(x["body"] or ""),
                                  x["ts"] or datetime.max.replace(tzinfo=timezone.utc)))
        lead = items[0]
        lead_s2 = next((x for x in items if x["s2"]), None)
        out.append({"event_id": eid, "lead": lead, "items": items,
                    "n_sources": len({x["source"] for x in items}),
                    "rel": max(x["rel"] or 0 for x in items),
                    "rtype": lead["rtype"], "s2": (lead_s2 or {}).get("s2"),
                    "value": (lead_s2 or {}).get("value"),
                    "ts": max((x["ts"] for x in items if x["ts"]), default=None)})
    return out


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
            parts.append(
                f"[{k}] Источник: {lead['source']}, {ts}. Заголовок: {lead['title']}\n"
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


def day_events(window_h: float = 26, reg_window_h: float = 96) -> list[dict]:
    """Оценённые события для выпуска: окно суток (регуляторика — 4 дня: решение
    пятницы доживает до понедельника), ещё не опубликованные в прошлые дни."""
    today = datetime.now(MSK).date()
    evs = _event_rows(since_h=reg_window_h)
    out = []
    for e in evs:
        if e["s2"] is None:
            continue
        cat = (e["s2"] or {}).get("category")
        age_h = ((datetime.now(timezone.utc) - e["ts"]).total_seconds() / 3600.0) if e["ts"] else 0
        limit_h = reg_window_h if cat in ("enforcement", "regulation", "court") else window_h
        if age_h > limit_h:
            continue
        pub = [x.get("published_on") for x in e["items"]]
        with db.session() as s:
            prev = s.execute(text("""SELECT min(published_on) FROM news_item
                                     WHERE event_id = :e AND published_on IS NOT NULL"""),
                             {"e": e["event_id"]}).scalar()
        if prev and prev < today:
            continue
        del pub
        e["group"] = _GROUP_OF.get(cat, "other")
        out.append(e)
    out.sort(key=lambda e: (-(e["value"] or 0), -e["n_sources"]))
    return out


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
                                          'ещё не одно событие. Ответ JSON: {"items":[{"n":1,"v":"same"}]}',
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


def mark_published(event_ids: list[int]) -> None:
    if not event_ids:
        return
    with db.session() as s:
        s.execute(text("""UPDATE news_item SET published_on = (now() AT TIME ZONE 'Europe/Moscow')::date
                          WHERE event_id = ANY(:e) AND published_on IS NULL"""), {"e": event_ids})


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
    return {"sources": src, "items_24h": int(agg[0]), "relevant_24h": int(agg[1]),
            "strong_24h": int(agg[2]), "events_24h": int(agg[3]), "yield_14d": yld}
