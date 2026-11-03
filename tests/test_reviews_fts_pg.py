"""Поиск по словам на настоящем to_tsvector (rag/bankiru_fts.py): фразы, словоформы,
стоп-слова. Моки здесь бесполезны — ошибки были именно в том, как Postgres разбирает
русский текст («навязали страховку» находила 0, «зачисляют» не находила «зачислили»).

Запуск: пустая база и FTS_PG_TEST_URL=postgresql+psycopg://user@127.0.0.1:5432/db pytest
tests/test_reviews_fts_pg.py — таблицу тест создаёт сам. Без переменной тесты пропускаются.
"""
from __future__ import annotations

import os

import pytest

from bank_audit import db
from bank_audit.rag import bankiru_fts as F

PG = os.getenv("FTS_PG_TEST_URL")
pytestmark = pytest.mark.skipif(not PG, reason="нужна живая база: FTS_PG_TEST_URL")

DOCS = {
    "u1": "Мне навязали страховку при оформлении кредита, отказаться не дали.",
    "u2": "Страховку не навязывали, но кредит одобрили с опозданием на неделю.",
    "u3": "Запретили переводы самому себе между своими счетами в другой банк.",
    "u4": "Переводы себе самому в другой банк больше не проходят.",
    "u5": "Выручку по эквайрингу не зачисляют третий день, деньги покупателей зависли.",
    "u6": "Банк зачислил выручку с опозданием, а эквайринг работает с перебоями.",
    "u7": "Страховка жизни навязана сотрудником, сумма кредита выросла.",
}


@pytest.fixture
def pg(monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    eng = create_engine(PG, future=True)
    with eng.begin() as c:
        c.execute(text("""DROP TABLE IF EXISTS review_index;
            CREATE TABLE review_index (url text PRIMARY KEY, review_id bigint, source text, bank text,
                                       product text, dt timestamptz DEFAULT now(), city text, tsv tsvector)"""))
        for k, (u, t) in enumerate(DOCS.items()):
            c.execute(text("""INSERT INTO review_index (url, review_id, source, bank, tsv)
                              VALUES (:u, :i, 'bankiru', 'Сбербанк', to_tsvector('russian', :t))"""),
                      {"u": u, "i": k + 1, "t": t})
    monkeypatch.setattr(db, "_Session", sessionmaker(bind=eng, expire_on_commit=False, future=True))
    yield eng
    eng.dispose()


def _urls(rows):
    return sorted(r["url"] for r in rows)


def test_near_words_with_word_forms(pg):
    rows, capped = F.search_near([["навязали", "страховку"]])
    # «навязали страховку» ↔ «страховка навязана»: падеж и залог — одна основа.
    # Известный пробел: вид глагола («навязывали», u2) — другая основа, пары видов
    # связывает только раскрытие запроса (волна 2: семьи слов)
    assert _urls(rows) == ["u1", "u7"] and not capped
    rows, _ = F.search_near([["не", "зачисляют", "выручку"]])
    assert "u5" in _urls(rows) and "u6" in _urls(rows)          # «зачислил выручку» — та же основа
    # служебные слова запроса тоже занимают позиции: «выручку по эквайрингу не зачисляют»
    rows, _ = F.search_near([["зачисляют", "выручку"]])
    assert _urls(rows) == ["u6"]
    rows, _ = F.search_near([["зачисляют", "выручку"]], spans=[3])
    assert _urls(rows) == ["u5", "u6"]
    rows, _ = F.search_near([["выручка", "банкомат"]])
    assert rows == []                                            # «банкомата» в текстах нет


def test_phrases_are_exact_and_all_required(pg):
    meta: dict = {}
    rows, _ = F.search_phrases(["переводы самому себе"], _meta=meta)
    assert _urls(rows) == ["u3"] and meta == {}                  # «себе самому» — другой порядок
    rows, _ = F.search_phrases(["навязали страховку"])
    assert _urls(rows) == ["u1"]
    rows, _ = F.search_phrases(["навязали страховку", "кредита"])
    assert _urls(rows) == ["u1"]
    rows, _ = F.search_phrases(["навязали страховку", "эквайринг"])
    assert rows == []
    meta = {}
    rows, _ = F.search_phrases(["не без"], _meta=meta)            # одни стоп-слова
    assert rows == [] and meta["stop_phrases"] == ["не без"]
    meta = {}
    rows, _ = F.search_phrases(["выручку", "без"], _meta=meta)    # пустая фраза не обнуляет остальные
    assert _urls(rows) == ["u5", "u6"] and meta["stop_phrases"] == ["без"]


def test_failure_is_reported(pg, monkeypatch):
    class Boom:
        def __enter__(self): raise RuntimeError("нет базы")
        def __exit__(self, *a): return False
    monkeypatch.setattr(F.db, "session", lambda: Boom())
    meta: dict = {}
    assert F.search_near([["перевод"]], _meta=meta) == ([], False) and meta["failed"] is True
    meta = {}
    assert F.search_phrases(["перевод"], _meta=meta) == ([], False) and meta["failed"] is True
