"""Память исследования: факты отчёта переживают прогон.

ЗАЧЕМ. Аудит 11.11: 270 из 357 отчётов — не первый вопрос разговора, и каждый
собирался с нуля. «Сравни потребкредиты у трёх банков», затем «подробнее про
требования к заёмщику» — это два одинаковых сбора по семь минут, хотя ответ на
второй вопрос уже лежал в фактах первого.

Теперь проверенные факты отчёта сохраняются (таблица report_memory) вместе с
планом и сведениями об источниках. Следующий вопрос в том же разговоре, если
планировщик считает его продолжением темы, начинает с них: общий сбор по сети
не идёт, руководитель исследования (lead.py) решает, чего не хватает, и
добирает только это. Память накапливается: в неё уходит объединённый набор.

Свежесть важнее скорости, поэтому: продолжается только отчёт не старше трёх
дней; сводки AuditLens (жалобы, рынок, схемы, новости) из памяти не берутся —
читаются заново; изменчивые страницы базы знаний перечитываются вживую
(own_data.is_volatile); в интерфейсе видно, что отчёт продолжает прошлый, и
есть «собрать заново» (fresh=True) — тогда память не используется вовсе.

Чего здесь нет: текста страниц (хранятся только цитаты фактов — их и сверяет
проверка чисел) и выводов модели (только факты с источником).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Продолжать можно только свежий отчёт: тарифы и число жалоб меняются. Старше —
# обычный сбор с нуля (владелец 11.11: «может, что-то изменилось»).
MAX_AGE_DAYS = int(os.getenv("GPTR_MEMORY_DAYS", "3"))
# Сводки AuditLens (жалобы, витрина «Рынок», схемы, новости) из памяти не берём:
# они читаются из базы заново за секунды и всегда на сегодня.
_REFRESHED_KINDS = {"complaints", "market", "loopholes", "loophole", "news"}
MAX_FACTS = int(os.getenv("GPTR_MEMORY_FACTS", "1500"))
EVENT = "memory"          # служебное событие потока: до клиента не доходит


def enabled() -> bool:
    return os.getenv("GPTR_MEMORY", "1").lower() in ("1", "true", "yes")


# ── Запись ───────────────────────────────────────────────────────────────────
def dump(registry, state, plan, question: str, attributes=()) -> dict:
    """Снимок прогона: факты, сведения об их источниках, как был понят вопрос."""
    facts = [{"s": f.subject, "a": f.attribute, "v": f.value, "u": f.unit,
              "q": f.verbatim, "url": f.url, "st": f.stance, "d": f.date,
              "sup": f.support}
             for f in registry.facts[:MAX_FACTS]]
    sources: dict[str, dict] = {}
    for url in {f["url"] for f in facts}:
        text = state.pages.get(url) or ""
        first = next((ln.strip().lstrip("# ") for ln in text.splitlines() if ln.strip()), "")
        meta: dict = {"title": first[:140], "date": state.page_dates.get(url, "")}
        for key, store in (("own", state.own_meta), ("kb", state.kb_meta),
                           ("review", state.review_meta)):
            if url in store:
                meta[key] = store[url]
        sources[url] = meta
    return {
        "v": 1, "question": question,
        "plan": {
            "task": getattr(plan, "task", ""), "scope": getattr(plan, "scope", ""),
            "product": getattr(plan, "product", ""),
            "subjects": list(getattr(plan, "subjects", None) or []),
            "subject_labels": dict(getattr(plan, "subject_labels", None) or {}),
            "intent_summary": getattr(plan, "intent_summary", ""),
            "period": getattr(plan, "period", ""),
            "attributes": [str(a) for a in attributes],
        },
        "facts": facts, "sources": sources,
    }


def save(report_id: int, session_id: int | None, username: str, mem: dict) -> bool:
    """Кладёт снимок в базу. Любая ошибка — молча: отчёт уже сохранён."""
    if not (report_id and mem and mem.get("facts")):
        return False
    from sqlalchemy import text

    from ... import db
    try:
        with db.session() as s:
            s.execute(text("SET LOCAL statement_timeout = '8s'"))
            s.execute(text("""
                INSERT INTO report_memory (report_id, session_id, username, question,
                                           plan, facts, sources, n_facts)
                VALUES (:r, :s, :u, :q, CAST(:p AS jsonb), CAST(:f AS jsonb),
                        CAST(:src AS jsonb), :n)
                ON CONFLICT (report_id) DO UPDATE
                   SET plan = EXCLUDED.plan, facts = EXCLUDED.facts,
                       sources = EXCLUDED.sources, n_facts = EXCLUDED.n_facts
            """), {"r": report_id, "s": session_id, "u": username,
                   "q": (mem.get("question") or "")[:2000],
                   "p": json.dumps(mem.get("plan") or {}, ensure_ascii=False, default=str),
                   "f": json.dumps(mem.get("facts") or [], ensure_ascii=False, default=str),
                   "src": json.dumps(mem.get("sources") or {}, ensure_ascii=False, default=str),
                   "n": len(mem.get("facts") or [])})
        return True
    except Exception as e:  # noqa: BLE001 — память необязательна
        log.warning("память исследования: не сохранена — %s", str(e)[:160])
        return False


# ── Чтение ───────────────────────────────────────────────────────────────────
def latest(session_id: int | None, username: str | None) -> dict | None:
    """Последний снимок этого разговора, не старше MAX_AGE_DAYS."""
    if not (enabled() and session_id and username):
        return None
    from sqlalchemy import text

    from ... import db
    try:
        with db.session() as s:
            s.execute(text("SET LOCAL statement_timeout = '8s'"))
            row = s.execute(text("""
                SELECT m.report_id, m.question, m.plan, m.facts, m.sources, m.created_at,
                       r.title
                  FROM report_memory m
                  LEFT JOIN report r ON r.report_id = m.report_id
                 WHERE m.session_id = :s AND m.username = :u
                   AND m.created_at > now() - make_interval(days => :d)
                 ORDER BY m.created_at DESC
                 LIMIT 1"""), {"s": session_id, "u": username, "d": MAX_AGE_DAYS}).mappings().first()
    except Exception as e:  # noqa: BLE001 — таблицы может не быть до миграции
        log.info("память исследования: не прочитана — %s", str(e)[:120])
        return None
    if not row or not isinstance(row["facts"], list) or not row["facts"]:
        return None
    return {"report_id": row["report_id"], "question": row["question"] or "",
            "title": row["title"] or "", "plan": row["plan"] or {},
            "facts": row["facts"], "sources": row["sources"] or {},
            "created_at": row["created_at"]}


def age_days(mem: dict) -> int:
    at = mem.get("created_at")
    if not isinstance(at, datetime):
        return 0
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - at).days)


def when(mem: dict) -> str:
    at = mem.get("created_at")
    return at.strftime("%d.%m.%Y") if isinstance(at, datetime) else ""


# ── Для планировщика ─────────────────────────────────────────────────────────
def brief(mem: dict, top: int = 14) -> str:
    """Что уже собрано в этом разговоре — несколько строк для планировщика."""
    plan = mem.get("plan") or {}
    counts: dict[str, int] = {}
    for f in mem.get("facts") or []:
        counts[f.get("a") or ""] = counts.get(f.get("a") or "", 0) + 1
    attrs = sorted((a for a in counts if a), key=lambda a: -counts[a])[:top]
    labels = plan.get("subject_labels") or {}
    subjects = [labels.get(s, s) for s in plan.get("subjects") or []]
    lines = [
        f"Вопрос: {(mem.get('question') or '')[:300]}",
        f"Предмет: {plan.get('product') or '—'}; организации: {', '.join(subjects) or 'не названы'}",
        f"Собрано фактов: {len(mem.get('facts') or [])}, источников: {len(mem.get('sources') or {})}"
        + (f"; отчёт от {when(mem)}" if when(mem) else ""),
        "Характеристики (число фактов): " + "; ".join(f"{a} — {counts[a]}" for a in attrs),
    ]
    return "\n".join(lines)


_WORD = re.compile(r"[a-zа-яё]{4,}", re.I)


def _stems(*texts: str) -> set[str]:
    return {w.lower().replace("ё", "е")[:5] for t in texts for w in _WORD.findall(t or "")}


def same_topic(plan, mem: dict) -> bool:
    """Тот же предмет, что в прошлом отчёте: общий корень в названии продукта.

    Модель уже сказала «это продолжение»; здесь — страховка от очевидной
    ошибки (вклады после ипотеки). Без названного продукта с обеих сторон
    верим модели: вопросы о рынке и нормах продукта могут не называть.
    """
    prior = mem.get("plan") or {}
    new = _stems(getattr(plan, "product", "") or "")
    old = _stems(prior.get("product") or "")
    if not new or not old:
        return True
    return bool(new & old)


def new_subjects(plan, mem: dict) -> list[str]:
    """Организации нового вопроса, по которым в памяти нет ни одного факта."""
    known = {f.get("s") for f in mem.get("facts") or [] if f.get("s")}
    return [s for s in (getattr(plan, "subjects", None) or []) if s and s not in known]


# ── Загрузка в прогон ────────────────────────────────────────────────────────
def preload(registry, state, mem: dict) -> tuple[int, dict]:
    """Кладёт факты памяти в реестр прогона. Возвращает (сколько, счётчики опоры).

    Текста страниц в памяти нет, поэтому «страницей» источника становятся его
    цитаты: проверка чисел отчёта сверяет их с цитатами — строже, чем со всей
    страницей. Факты уже прошли критика в своём прогоне и повторно не судятся.
    """
    by_url: dict[str, list[str]] = {}
    support = {"exact": 0, "close": 0}
    n = 0
    sources = mem.get("sources") or {}
    for f in mem.get("facts") or []:
        url, quote = f.get("url") or "", f.get("q") or ""
        if not (url and quote):
            continue
        own = (sources.get(url) or {}).get("own")
        if isinstance(own, dict) and own.get("kind") in _REFRESHED_KINDS:
            continue                      # придут свежими из собственных данных
        fact = registry.add(subject=f.get("s") or "", attribute=f.get("a") or "",
                            value=f.get("v") or "", unit=f.get("u") or "",
                            verbatim=quote, url=url, stance=f.get("st") or "observed")
        fact.date = f.get("d") or ""
        fact.support = f.get("sup") or ""
        support["exact" if fact.support in ("", "дословно") else "close"] += 1
        by_url.setdefault(url, []).append(quote)
        n += 1
    for url, quotes in by_url.items():
        meta = (mem.get("sources") or {}).get(url) or {}
        if url not in state.pages:
            state.pages[url] = "\n".join(filter(None, [meta.get("title") or "", *quotes]))
        if meta.get("date"):
            state.page_dates.setdefault(url, meta["date"])
        for key, store in (("own", state.own_meta), ("kb", state.kb_meta),
                           ("review", state.review_meta)):
            if isinstance(meta.get(key), dict):
                store.setdefault(url, meta[key])
    return n, support


async def without_event(inner):
    """Поток без служебного события памяти — для пути, где отчёт не сохраняется
    (база недоступна): клиенту сотни килобайт фактов не нужны."""
    marker = f'"type": "{EVENT}"'
    async for ev in inner:
        if marker in ev[:40]:
            continue
        yield ev
