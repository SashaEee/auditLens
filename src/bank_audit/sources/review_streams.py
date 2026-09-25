"""Отзывы площадок из их JSON-выдачи: banki.ru и sravni.ru.

Заменяют HTML-сборщики, которые к сентябрю 2026 почти перестали приносить новое:

* banki.ru разбирался регулярками по странице, у 23 из 25 банков адрес был
  неверный (404) или соединение рвалось, а ответ банка и статус «проблема
  решена» не сохранялись вовсе. JSON-выдача `/services/responses/list/ajax/`
  отдаёт отзыв целиком: оценку, текст, дату, признак проверки, ответ банка и
  «решено». Негатив (1–2★) и так приходит во внешнем корпусе, поэтому здесь
  главное — ответ банка и «решено» по жалобам Сбера, плюс 3★, которых в корпусе
  нет. Сбор — только Сбер: объект аудита, ~25 отзывов в день.
* sravni.ru читался со страниц `/bank/<alias>/otzyvy/`, но у Сбера и других
  банков алиас был неверный, и страница отдавала общую витрину популярных
  отзывов ВСЕХ банков: отзывы Т-Банка, Ozon, Альфы записывались Сберу.
  API `/proxy-reviews/reviews/` с идентификатором банка из справочника площадки
  отдаёт отзывы именно этого банка по дате.

Запись — в `review` с обновлением при повторной встрече: ответ банка и
«решено» появляются через дни после публикации.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import text

from .. import db

log = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
BANKI_LIST = "https://www.banki.ru/services/responses/list/ajax/"
SRAVNI_API = "https://www.sravni.ru/proxy-reviews/reviews/"
SRAVNI_ORGS_PAGE = "https://www.sravni.ru/banki/otzyvy/"
_PAUSE_S = float(os.getenv("REVIEW_STREAM_PAUSE_S", "1.5"))
_TAG = re.compile(r"<[^>]+>")


def _client() -> httpx.Client:
    return httpx.Client(http2=False, follow_redirects=True,
                        timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
                        headers={"User-Agent": _UA, "Accept": "application/json, text/plain, */*",
                                 "Accept-Language": "ru-RU,ru;q=0.9",
                                 "X-Requested-With": "XMLHttpRequest"})


def _get_json(c: httpx.Client, url: str, params: dict) -> dict:
    last = None
    for attempt in range(3):
        try:
            r = c.get(url, params=params)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except (httpx.TransportError, ValueError) as e:
            last = f"{type(e).__name__}: {str(e)[:100]}"
        time.sleep(4 * (attempt + 1))
    raise RuntimeError(last or "нет ответа")


def clean_text(s: str | None) -> str:
    s = html.unescape(_TAG.sub(" ", s or "")).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", re.sub(r"\s*\n\s*", "\n", s)).strip()


def _bool(v: Any) -> bool | None:
    if v in (True, "True", "true", 1, "1"):
        return True
    if v in (False, "False", "false", 0, "0"):
        return False
    return None


def _state_get(k: str) -> str | None:
    with db.session() as s:
        return s.execute(text("SELECT v FROM review_index_state WHERE k = :k"), {"k": k}).scalar()


def _state_set(k: str, v: str) -> None:
    with db.session() as s:
        s.execute(text("""INSERT INTO review_index_state (k, v, updated_at) VALUES (:k, :v, now())
                          ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = now()"""),
                  {"k": k, "v": v})


def _run_start(source: str, target: str) -> int | None:
    try:
        with db.session() as s:
            return s.execute(text("""INSERT INTO extraction_run (source, target_name, started_at, status)
                                     VALUES (:s, :t, now(), 'running') RETURNING run_id"""),
                             {"s": source, "t": target}).scalar()
    except Exception:  # noqa: BLE001 — журнал не должен ронять сбор
        return None


def _run_finish(run_id: int | None, status: str, seen: int, written: int, error: str | None = None,
                meta: dict | None = None) -> None:
    if not run_id:
        return
    try:
        with db.session() as s:
            s.execute(text("""UPDATE extraction_run SET finished_at = now(), status = :st,
                                     items_seen = :se, items_written = :wr, error = :er,
                                     meta = CAST(:m AS jsonb) WHERE run_id = :r"""),
                      {"st": status, "se": seen, "wr": written, "er": (error or "")[:500] or None,
                       "m": json.dumps(meta or {}, ensure_ascii=False), "r": run_id})
    except Exception:  # noqa: BLE001
        pass


def upsert(session, *, source: str, rid: str, url: str, bank_name: str, posted_at: datetime | None,
           rating: float | None, title: str | None, body: str, raw: dict, status: str | None) -> bool:
    """Вставка или обновление отзыва. True — новый. Оценка, статус и поля raw
    (ответ банка, «решено») обновляются при каждой встрече: они меняются уже
    после публикации."""
    from ..normalizer.reviews import resolve_bank
    bank_id = resolve_bank(session, bank_name)
    row = session.execute(text("""
        INSERT INTO review (source, source_review_id, source_url, bank_id, posted_at, rating,
                            title, text, status, raw)
        VALUES (:s, :rid, :url, :b, :pa, :r, :t, :tx, :st, CAST(:raw AS jsonb))
        ON CONFLICT (source, source_review_id) DO UPDATE SET
            bank_id = EXCLUDED.bank_id, source_url = EXCLUDED.source_url,
            rating = coalesce(EXCLUDED.rating, review.rating),
            title = coalesce(EXCLUDED.title, review.title),
            text = CASE WHEN length(EXCLUDED.text) >= 20 THEN EXCLUDED.text ELSE review.text END,
            status = coalesce(EXCLUDED.status, review.status),
            raw = coalesce(review.raw, '{}'::jsonb) || EXCLUDED.raw
        RETURNING (xmax = 0)
    """), {"s": source, "rid": rid, "url": url, "b": bank_id, "pa": posted_at, "r": rating,
           "t": (title or None), "tx": body, "st": status,
           "raw": json.dumps(raw, ensure_ascii=False, default=str)}).scalar()
    return bool(row)


# ── banki.ru ────────────────────────────────────────────────────────────────

def _banki_row(session, it: dict, bank_name: str) -> bool:
    rid = str(it.get("id"))
    try:
        posted = datetime.strptime(str(it.get("dateCreate"))[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)
    except ValueError:
        posted = None
    try:
        grade = float(it.get("grade")) if it.get("grade") not in (None, "", "None") else None
    except ValueError:
        grade = None
    countable = _bool(it.get("isCountable"))
    answer = clean_text(it.get("agentAnswerText")) or None
    raw = {"grade": grade, "countable": countable, "resolved": _bool(it.get("resolutionIsApproved")),
           "answer": answer, "company_code": (it.get("company") or {}).get("code"),
           "comments": it.get("commentCount"), "seen_at": datetime.now(MSK).isoformat()}
    return upsert(session, source="banki_reviews", rid=rid,
                  url=f"https://www.banki.ru/services/responses/bank/response/{rid}/",
                  bank_name=bank_name, posted_at=posted, rating=grade, title=it.get("title"),
                  body=clean_text(it.get("text")), raw=raw,
                  status="countable" if countable else ("rejected" if countable is False else "unchecked"))


def banki_sync(bank_code: str = "sberbank", bank_name: str = "Сбербанк",
               fresh_pages: int = 10, status_days: int = 45, status_pages: int = 60) -> dict:
    """Свежие отзывы банка (включая непроверенные) до прошлой встречи и
    проверенные за status_days — у них за это время появляются ответ банка и
    «решено»."""
    out = {"fresh": 0, "status": 0, "new": 0, "pages": 0}
    run = _run_start("banki_reviews", f"json:{bank_code}")
    wm_key = f"banki_json_last_id:{bank_code}"
    last = int(_state_get(wm_key) or 0)
    top = last
    err = None
    try:
        with _client() as c:
            for page in range(1, fresh_pages + 1):
                d = _get_json(c, BANKI_LIST, {"page": page, "bank": bank_code, "is_countable": "off"})
                out["pages"] += 1
                items = d.get("data") or []
                with db.session() as s:
                    for it in items:
                        out["new"] += _banki_row(s, it, bank_name)
                out["fresh"] += len(items)
                ids = [int(it["id"]) for it in items if str(it.get("id", "")).isdigit()]
                top = max([top] + ids)
                if not items or not d.get("hasMorePages") or (ids and min(ids) <= last):
                    break
                time.sleep(_PAUSE_S)
            _state_set(wm_key, str(top))
            border = datetime.now(MSK) - timedelta(days=status_days)
            for page in range(1, status_pages + 1):
                time.sleep(_PAUSE_S)
                d = _get_json(c, BANKI_LIST, {"page": page, "bank": bank_code})
                out["pages"] += 1
                items = d.get("data") or []
                with db.session() as s:
                    for it in items:
                        out["new"] += _banki_row(s, it, bank_name)
                out["status"] += len(items)
                oldest = min((str(it.get("dateCreate")) for it in items), default="")
                if not items or not d.get("hasMorePages") or oldest < border.strftime("%Y-%m-%d"):
                    break
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        log.warning("banki_sync %s: %s", bank_code, err)
    _run_finish(run, "failed" if err and not out["fresh"] else "ok",
                out["fresh"] + out["status"], out["new"], err, out)
    return out


# ── sravni.ru ───────────────────────────────────────────────────────────────

def sravni_orgs(c: httpx.Client | None = None) -> list[dict]:
    """Справочник организаций площадки (id, alias, name) — из страницы отзывов."""
    own = c is None
    c = c or _client()
    try:
        r = c.get(SRAVNI_ORGS_PAGE, headers={"Accept": "text/html"})
        m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        st = json.loads(m.group(1))["props"]["initialReduxState"]
        orgs = (st.get("organizations") or {}).get("organizationsList") or []
        if isinstance(orgs, dict):
            orgs = orgs.get("items") or list(orgs.values())
        return [{"id": o.get("id"), "alias": o.get("alias"), "name": o.get("name")}
                for o in orgs if isinstance(o, dict) and o.get("id") and o.get("name")]
    finally:
        if own:
            c.close()


def _sravni_match(orgs: list[dict], canon: str) -> dict | None:
    from ..rag.bankiru_reviews import resolve_bank as canon_of
    for o in orgs:
        if o["name"] == canon or canon_of(o["name"]) == canon:
            return o
    return None


def _sravni_row(session, it: dict, org: dict) -> bool:
    rid = str(it.get("id"))
    try:
        posted = datetime.fromisoformat(str(it.get("date")).replace("Z", "+00:00"))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=MSK)
    except ValueError:
        posted = None
    rating = it.get("rating")
    rating = float(rating) if rating not in (None, 0, "0", "") else None
    loc = it.get("locationData") or {}
    raw = {"review_object_id": org["id"], "problem_solved": _bool(it.get("problemSolved")),
           "company_response": _bool(it.get("hasCompanyResponse")), "tag": it.get("reviewTag"),
           "city": (loc.get("name") if isinstance(loc, dict) else None),
           "seen_at": datetime.now(MSK).isoformat()}
    return upsert(session, source="sravni_reviews", rid=rid,
                  url=f"https://www.sravni.ru/bank/{org['alias']}/otzyvy/{rid}/",
                  bank_name=org["name"], posted_at=posted, rating=rating, title=it.get("title"),
                  body=clean_text(it.get("text")), raw=raw, status=it.get("ratingStatus"))


def sravni_sync(banks: list[str], pages: int = 4, page_size: int = 50) -> dict:
    """Отзывы банков по дате до прошлой встречи (по номеру отзыва)."""
    out = {"banks": 0, "seen": 0, "new": 0, "unmatched": []}
    run = _run_start("sravni_reviews", "api")
    err = None
    try:
        with _client() as c:
            orgs = sravni_orgs(c)
            for canon in banks:
                org = _sravni_match(orgs, canon)
                if not org:
                    out["unmatched"].append(canon)
                    continue
                out["banks"] += 1
                wm_key = f"sravni_last_id:{org['id']}"
                last = int(_state_get(wm_key) or 0)
                top = last
                for page in range(pages):
                    time.sleep(_PAUSE_S)
                    d = _get_json(c, SRAVNI_API, {"filterBy": "all", "orderBy": "byDate", "pageIndex": page,
                                                   "pageSize": page_size, "reviewObjectId": org["id"],
                                                   "reviewObjectType": "banks"})
                    items = d.get("items") or []
                    with db.session() as s:
                        for it in items:
                            out["new"] += _sravni_row(s, it, org)
                    out["seen"] += len(items)
                    ids = [int(it["id"]) for it in items if str(it.get("id", "")).isdigit()]
                    top = max([top] + ids)
                    if not items or (ids and min(ids) <= last):
                        break
                _state_set(wm_key, str(top))
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        log.warning("sravni_sync: %s", err)
    _run_finish(run, "failed" if err and not out["seen"] else "ok", out["seen"], out["new"], err, out)
    return out


def sravni_fix_attribution() -> dict:
    """Старые отзывы sravni, собранные со страницы-витрины, записаны не тому
    банку. Настоящий банк лежит в raw.review_object_id — перепривязываем отзыв,
    ссылку, строку индекса и разметку (разметка про текст, её не переделываем)."""
    orgs = {o["id"]: o for o in sravni_orgs()}
    moved = unknown = 0
    from ..normalizer.reviews import resolve_bank
    from ..rag.bankiru_reviews import resolve_bank as canon_of
    with db.session() as s:
        rows = s.execute(text("""SELECT review_id, source_review_id, source_url, raw->>'review_object_id'
                                 FROM review WHERE source = 'sravni_reviews'""")).all()
        for review_id, rid, old_url, oid in rows:
            org = orgs.get(oid or "")
            if not org:
                unknown += 1
                continue
            new_url = f"https://www.sravni.ru/bank/{org['alias']}/otzyvy/{rid}/"
            bank_id = resolve_bank(s, org["name"])
            s.execute(text("UPDATE review SET bank_id = :b, source_url = :u WHERE review_id = :r"),
                      {"b": bank_id, "u": new_url, "r": review_id})
            if new_url != old_url:
                s.execute(text("""UPDATE review_annotation SET url = :n WHERE url = :o
                                  AND NOT EXISTS (SELECT 1 FROM review_annotation x WHERE x.url = :n)"""),
                          {"n": new_url, "o": old_url})
                s.execute(text("""UPDATE review_index SET url = :n, bank = :bk WHERE url = :o
                                  AND NOT EXISTS (SELECT 1 FROM review_index x WHERE x.url = :n)"""),
                          {"n": new_url, "o": old_url, "bk": canon_of(org["name"]) or org["name"]})
                moved += 1
    return {"rows": len(rows), "moved": moved, "unknown_org": unknown}


def run_all() -> dict:
    """Суточный проход: banki.ru по Сберу, sravni по крупнейшим банкам."""
    res = {"banki": banki_sync()}
    try:
        from ..rag import reviews_dash as rd
        top = [b["bank"] for b in rd.banks()][:15]
    except Exception:  # noqa: BLE001
        top = ["Сбербанк", "ВТБ", "Альфа-Банк", "Т-Банк", "Газпромбанк", "Совкомбанк"]
    res["sravni"] = sravni_sync(top)
    return res
