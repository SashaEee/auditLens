"""Разовая переразметка базы знаний (аудит 03.10, ДАН-03/ДАН-04).

    python -m bank_audit.rag.kb_backfill            # пробный прогон, ничего не пишет
    python -m bank_audit.rag.kb_backfill --apply    # запись

1. Темы документов заново: адрес по новым правилам, а где он темы не даёт —
   заголовок и начало текста (rag/topics.py). Пишем только изменившиеся.
2. Фрагменты вне поиска (таблица document_chunk_excluded, migrations/091):
   • хвост «Элементы интерфейса» — меню и подписи кнопок;
   • меню агрегаторов, разобранные старым парсером: строка считается
     шаблонной, если она повторяется не меньше чем в TEMPLATE_DOCS
     страницах одного сайта-агрегатора; фрагмент из ≥ MIN_LINES строк, где
     шаблонных больше NAV_SHARE, — меню. Повторяемость в пределах сайта, а не
     длина строк: «Необходимые документы» конкретной страницы так не задеть.
     Страница — адрес (retriever.norm_url), а не версия: 20 перечитываний
     одной страницы не делают её текст «шаблоном сайта».
Повторный --apply даёт 0 изменений. Откат: TRUNCATE document_chunk_excluded;
темы — из таблицы document_topics_bak_20261004, куда --apply до записи
копирует прежние темы изменённых документов:
    UPDATE document d SET topics = b.topics FROM document_topics_bak_20261004 b
     WHERE b.document_id = d.document_id;
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict

from sqlalchemy import text

from .. import db
from .topics import classify_document

TEMPLATE_DOCS = 20
MIN_LINES = 5
NAV_SHARE = 0.6
_BULLET = re.compile(r"^[-•*·]\s*")


def _norm_line(line: str) -> str:
    return re.sub(r"\s+", " ", _BULLET.sub("", line.strip())).lower()[:120]


def _lines(text_: str) -> list[str]:
    return [x for x in (_norm_line(ln) for ln in (text_ or "").splitlines()) if len(x) >= 3]


def nav_chunks(chunks: list[tuple[int, str, str]]) -> list[int]:
    """chunks: [(chunk_id, адрес страницы, text)] одного сайта → id фрагментов-меню.
    Повторяемость строки — по разным СТРАНИЦАМ (адресам), не по версиям."""
    per_doc: dict[str, set[str]] = defaultdict(set)
    for _cid, page, t in chunks:
        for ln in set(_lines(t)):
            per_doc[ln].add(page)
    template = {ln for ln, ds in per_doc.items() if len(ds) >= TEMPLATE_DOCS}
    out = []
    for cid, _did, t in chunks:
        ls = _lines(t)
        if len(ls) >= MIN_LINES and sum(1 for x in ls if x in template) / len(ls) > NAV_SHARE:
            out.append(cid)
    return out


def run(apply: bool = False) -> dict:
    rep: dict = {"apply": apply}
    with db.session() as s:
        # 1. темы
        changed, examples, src_cnt = [], [], Counter()
        rows = s.execute(text("""
            SELECT document_id, url, title, left(content_text, 3000) head, topics
              FROM document ORDER BY document_id""")).all()
        for did, url, title, head, topics in rows:
            new, src = classify_document(url or "", title, head)
            if sorted(new or []) != sorted(topics or []):
                changed.append({"i": did, "t": new or None})
                src_cnt[src] += 1
                if len(examples) < 25:
                    examples.append({"url": (url or "")[:90], "old": topics, "new": new, "src": src})
        rep["topics_changed"] = len(changed)
        rep["topics_by_source"] = dict(src_cnt)
        rep["topics_examples"] = examples
        if apply and changed:
            s.execute(text("""CREATE TABLE IF NOT EXISTS document_topics_bak_20261004 (
                                  document_id BIGINT PRIMARY KEY, topics TEXT[])"""))
            s.execute(text("""INSERT INTO document_topics_bak_20261004 (document_id, topics)
                              SELECT document_id, topics FROM document
                               WHERE document_id = ANY(:ids)
                              ON CONFLICT (document_id) DO NOTHING"""),
                      {"ids": [c["i"] for c in changed]})
            s.execute(text("UPDATE document SET topics = :t WHERE document_id = :i"), changed)
        # 2а. хвост интерфейса
        rep["ui_tail"] = s.execute(text("""
            SELECT count(*) FROM document_chunk c
             WHERE c.headings_path LIKE '%Элементы интерфейса (не условия продукта)%'
               AND NOT EXISTS (SELECT 1 FROM document_chunk_excluded x
                                WHERE x.chunk_id = c.chunk_id)""")).scalar()
        if apply:
            s.execute(text("""
                INSERT INTO document_chunk_excluded (chunk_id, reason)
                SELECT chunk_id, 'ui_tail' FROM document_chunk
                 WHERE headings_path LIKE '%Элементы интерфейса (не условия продукта)%'
                ON CONFLICT DO NOTHING"""))
        # 2б. меню агрегаторов — по сайтам
        agg = s.execute(text("""
            SELECT st.domain, c.chunk_id, d.url, c.text
              FROM document_chunk c
              JOIN document d ON d.document_id = c.document_id
              JOIN source_trust st ON st.source_id = d.source_id
             WHERE st.kind = 'aggregator'
               AND NOT EXISTS (SELECT 1 FROM document_chunk_excluded x
                                WHERE x.chunk_id = c.chunk_id)""")).all()
        from .retriever import norm_url
        by_dom: dict[str, list] = defaultdict(list)
        for dom, cid, url, t in agg:
            by_dom[dom].append((cid, norm_url(url), t))
        nav, nav_by_dom, nav_examples, emptied = [], {}, [], []
        for dom, ch in by_dom.items():
            ids = nav_chunks(ch)
            nav += ids
            idset = set(ids)
            # страницы, у которых в поиске не останется ни одного фрагмента
            pages: dict[str, list[int]] = defaultdict(list)
            for cid, page, _t in ch:
                pages[page].append(cid)
            lost = [p for p, cs in pages.items() if all(c in idset for c in cs)]
            emptied += lost[:5]
            nav_by_dom[dom] = {"chunks": len(ch), "nav": len(ids), "pages": len(pages),
                               "pages_emptied": len(lost)}
            for cid, _p, t in ch:
                if cid in idset and len(nav_examples) < 10:
                    nav_examples.append({"domain": dom, "text": (t or "")[:160]})
        rep["nav_template"] = len(nav)
        rep["nav_pages_emptied_examples"] = emptied[:25]
        rep["nav_by_domain"] = nav_by_dom
        rep["nav_examples"] = nav_examples
        if apply and nav:
            s.execute(text("""INSERT INTO document_chunk_excluded (chunk_id, reason)
                              VALUES (:c, 'nav_template') ON CONFLICT DO NOTHING"""),
                      [{"c": c} for c in nav])
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    from ..config import Settings
    db.init(Settings.load())
    print(json.dumps(run(apply=args.apply), ensure_ascii=False, default=str, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
