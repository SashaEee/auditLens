"""Схемы из данных: описания случаев (llm_read) → группы по смыслу → название моделью.

Не закрытый список категорий, а то, что реально пишут клиенты: описания «что произошло»
переводятся в векторы (bge-m3, как в «Аудите отзывов»), группируются k-средними по
косинусу, модель называет каждую группу и оценивает, интересна ли она аудиту отделений.
Группа, у которой большая часть случаев — за последние 90 дней, помечается «новая».

Запуск: python -m bank_audit.vsp schemes. Результат — vsp_scheme и vsp_scheme_member.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import timedelta

from sqlalchemy import Column, DateTime, Float, Integer, String, Table, Text, select

from ..models import utcnow
from ..storage import Store, meta, reviews
from .llm_read import PROMPT_VERSION, Reader, review_llm

log = logging.getLogger(__name__)

scheme_t = Table(
    "vsp_scheme", meta,
    Column("version", String(40), primary_key=True),      # дата сборки
    Column("id", Integer, primary_key=True),
    Column("name", Text), Column("description", Text),
    Column("relevance", Integer),                          # 0–3: насколько это про возможные нарушения в отделении
    Column("check", Text),                                 # что проверить (JSON-список)
    Column("benign", Text),                                # чем может объясняться безобидно (JSON-список)
    Column("size", Integer), Column("recent", Integer), Column("novel", Integer),
    Column("examples_json", Text),
    Column("at", DateTime(timezone=True)),
)
scheme_member = Table(
    "vsp_scheme_member", meta,
    Column("version", String(40), primary_key=True),
    Column("review_id", String(160), primary_key=True),
    Column("scheme_id", Integer),
    Column("sim", Float),
)

NAME_SYSTEM = """Ты — руководитель внутреннего аудита розничного банка. Тебе показывают группу похожих историй из отзывов
клиентов об отделениях банка (уже пересказанных нейтрально). Дай группе название простыми словами и оцени её.
Верни JSON: {"name": "3–7 слов, понятно аудитору", "description": "1–2 предложения: что происходит",
"relevance": 0–3 (3 — возможное мошенничество или злоупотребление сотрудников; 2 — нарушение прав клиентов;
1 — процесс; 0 — не про нарушения), "check": ["что проверить в отделении", ...до 4],
"benign": ["чем может объясняться безобидно", ...до 3]}"""


def _kmeans(X, k: int, iters: int = 30, seed: int = 7):
    import numpy as np
    rng = np.random.default_rng(seed)
    C = X[rng.choice(len(X), size=k, replace=False)]
    lab = np.zeros(len(X), dtype=int)
    for _ in range(iters):
        sims = X @ C.T
        new = sims.argmax(1)
        if (new == lab).all() and _ > 0:
            break
        lab = new
        for j in range(k):
            m = X[lab == j]
            if len(m):
                c = m.mean(0)
                C[j] = c / (np.linalg.norm(c) or 1)
    return lab, (X * C[lab]).sum(1)


def _embed(texts: list[str]) -> list[list[float]]:
    from ...rag.embedder import embed_batch
    return embed_batch(texts)


async def build(store: Store, *, k: int | None = None, min_relevance_rows: int = 30) -> dict:
    meta.create_all(store.engine, tables=[scheme_t, scheme_member])
    with store.engine.connect() as c:
        rows = [dict(r) for r in c.execute(select(
            review_llm.c.review_id, review_llm.c.summary, review_llm.c.scheme, review_llm.c.severity,
            reviews.c.review_date).join(reviews, reviews.c.review_id == review_llm.c.review_id).where(
            review_llm.c.prompt_version == PROMPT_VERSION, review_llm.c.relevant == 1)).mappings()]
    rows = [r for r in rows if (r["summary"] or "").strip()]
    if len(rows) < min_relevance_rows:
        return {"skipped": f"мало случаев ({len(rows)})"}
    import numpy as np
    X = np.array(_embed([f"{r['scheme']}. {r['summary']}" for r in rows]), dtype="float32")
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
    k = k or max(6, min(60, int(math.sqrt(len(rows) / 2))))
    lab, sim = _kmeans(X, k)
    now = utcnow()
    recent_cut = now - timedelta(days=90)
    version = now.strftime("%Y-%m-%d %H:%M")
    rd = Reader(system=NAME_SYSTEM)
    groups = []
    for j in range(k):
        idx = [i for i in range(len(rows)) if lab[i] == j]
        if not idx:
            continue
        idx.sort(key=lambda i: -sim[i])
        sample = idx[:15]
        prompt = "Истории группы:\n" + "\n".join(f"- {rows[i]['scheme']}: {rows[i]['summary']}" for i in sample)
        d, _ = await rd.ask(prompt)
        d = d if isinstance(d, dict) else {}
        recent = sum(1 for i in idx if rows[i]["review_date"] and
                     (rows[i]["review_date"] if rows[i]["review_date"].tzinfo else rows[i]["review_date"].replace(
                         tzinfo=recent_cut.tzinfo)) >= recent_cut)
        groups.append({"id": j, "name": str(d.get("name") or rows[idx[0]]["scheme"] or "Без названия")[:200],
                       "description": str(d.get("description") or "")[:1000],
                       "relevance": int(d.get("relevance") or 0) if str(d.get("relevance", "")).isdigit() else 0,
                       "check": json.dumps(d.get("check") or [], ensure_ascii=False),
                       "benign": json.dumps(d.get("benign") or [], ensure_ascii=False),
                       "size": len(idx), "recent": recent, "novel": int(len(idx) >= 3 and recent / len(idx) >= 0.5),
                       "examples_json": json.dumps([rows[i]["review_id"] for i in sample[:8]]), "at": now,
                       "version": version})
    with store.engine.begin() as c:
        c.execute(scheme_t.insert(), groups)
        c.execute(scheme_member.insert(), [{"version": version, "review_id": rows[i]["review_id"],
                                            "scheme_id": int(lab[i]), "sim": float(sim[i])} for i in range(len(rows))])
    res = {"version": version, "cases": len(rows), "groups": len(groups), "rub": round(rd.rub, 2),
           "novel": sum(g["novel"] for g in groups), "relevant_groups": sum(1 for g in groups if g["relevance"] >= 2)}
    log.info("схемы: %s", res)
    return res

