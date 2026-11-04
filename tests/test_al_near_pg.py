"""«Слова рядом» в SQL (migrations/106_al_near.sql, bankiru_fts.search_near_sql) на живом
Postgres. Главное — al_near обязана совпадать с near_window из Python до последнего
отзыва: поиск переезжает в базу ради счёта без потолка, а не ради другой меры.
Поэтому сверка идёт на сотнях случайных текстов из словаря со словоформами,
короткими основами и служебными словами, на настоящем русском to_tsvector.

Запуск: пустая база и AL_NEAR_PG_TEST_URL=postgresql+psycopg://user@127.0.0.1:5432/db
pytest -s tests/test_al_near_pg.py — таблицу и функцию тест создаёт сам; -s покажет
замер скорости. Без переменной тесты пропускаются: в общем прогоне базы нет.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import random
import time

import pytest

from bank_audit import db
from bank_audit.config import ROOT
from bank_audit.rag import bankiru_fts as F

PG = os.getenv("AL_NEAR_PG_TEST_URL")
pytestmark = pytest.mark.skipif(not PG, reason="нужна живая база: AL_NEAR_PG_TEST_URL")

# формы одного слова дают разные основы («зачисля»/«зачисл»), короткие основы («карт»,
# «банк», «счет») — только целиком, «интернет-банк» и «e-mail» — по три лексемы из одного
# слова, «2025» — основа не из букв (такую одиночную группу search_near_sql проверяет al_near)
VOCAB = ["2025", "e-mail", "банк", "банкомат", "банка", "карта", "карты", "картридж", "карточку",
         "отказ", "отказали", "отказе", "отказаться", "навязали", "навязывали", "страховку",
         "страховка", "страхование", "кредит", "кредита", "кредитный", "зачисляют", "зачислили",
         "зачисление", "выручку", "выручка", "эквайринг", "эквайрингу", "перевод", "переводы",
         "переводить", "счёт", "счета", "счетчик", "деньги", "денег", "интернет-банк", "ипотеки",
         "ипотека", "реструктуризации", "сотрудник", "клиент",
         "не", "по", "в", "с", "и", "на", "мне", "самому", "себе", "без", "уже"]

_TABLE = """
DROP TABLE IF EXISTS review_index;
CREATE TABLE review_index (url text PRIMARY KEY, review_id bigint NOT NULL, source text,
                           bank text, product text, dt timestamp, city text, tsv tsvector NOT NULL);
CREATE INDEX review_index_tsv ON review_index USING GIN (tsv);
"""


@pytest.fixture
def pg(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    eng = create_engine(PG, future=True)
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(_TABLE)
        cur.execute((ROOT / "migrations" / "106_al_near.sql").read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()
    monkeypatch.setattr(db, "_Session", sessionmaker(bind=eng, expire_on_commit=False, future=True))
    yield eng
    eng.dispose()


def _sql(sql: str, p: dict | None = None):
    from sqlalchemy import text
    with db.session() as s:
        r = s.execute(text(sql), p or {})
        return r.all() if r.returns_rows else None


def _put(docs: dict[str, str], **cols):
    """Отзывы по тексту; у каждого свой день, банк и продукт — для порядка и фильтров."""
    rows = [{"u": u, "i": k + 1, "t": t, "b": cols.get("bank") or ("Банк А" if k % 2 else "Банк Б"),
             "p": cols.get("product") or ("Кредиты" if k % 3 else "Карты"),
             "d": f"2026-{1 + k % 9:02d}-{1 + k % 27:02d}"}
            for k, (u, t) in enumerate(docs.items())]
    _sql("""INSERT INTO review_index (url, review_id, source, bank, product, dt, tsv)
            SELECT x->>'u', (x->>'i')::bigint, 'bankiru', x->>'b', x->>'p', (x->>'d')::timestamp,
                   to_tsvector('russian', x->>'t')
              FROM jsonb_array_elements(CAST(:rows AS jsonb)) x""",
         {"rows": json.dumps(rows, ensure_ascii=False)})


def _lexs(words: list[str]) -> list[str]:
    return list(_sql("SELECT tsvector_to_array(to_tsvector('russian', :t))",
                     {"t": " ".join(words)})[0][0] or [])


def _args(lexs: list[str], gap: int, words: int) -> tuple[list[str], int]:
    """Ключи и длина окна для al_near — ровно как их видит near_window."""
    keys = [x for x in dict.fromkeys(F.stem_key(x) for x in lexs) if x]
    return keys, max(len(keys), words) - 1 + gap


def _random_docs(rng: random.Random, n: int) -> dict[str, str]:
    docs = {f"r{k}": " ".join(rng.choice(VOCAB) for _ in range(rng.randint(3, 40)))
            for k in range(n)}
    # повторы одного слова — длинные списки позиций
    docs["rep"] = " ".join(["кредит"] * 30 + ["страховку"] + ["кредит"] * 30)
    docs["one"] = "страховка"
    return docs


def test_al_near_agrees_with_near_window(pg):
    rng = random.Random(20261104)
    docs = _random_docs(rng, 300)
    _put(docs)
    tsv = dict(_sql("SELECT url, tsv::text FROM review_index"))
    pos = {u: F._positions(t) for u, t in tsv.items()}
    seen = {"queries": 0, "true": 0, "false": 0, "all_keys_far": 0}
    for _ in range(300):
        words = [rng.choice(VOCAB) for _ in range(rng.randint(1, 4))]
        lexs = _lexs(words)
        if not lexs:
            continue
        gap, nwords = rng.randint(0, 3), len(words) + rng.choice([0, 0, 1, 3])
        keys, span = _args(lexs, gap, nwords)
        got = {u for (u,) in _sql("SELECT url FROM review_index WHERE al_near(tsv, :k, :s)",
                                   {"k": keys, "s": span})}
        want = {u for u, ps in pos.items() if F.near_window(ps, lexs, gap, words=nwords)}
        assert got == want, (words, keys, span, sorted(got ^ want)[:5])
        # отзывы со всеми словами, но не рядом: здесь al_near и решает
        wide = {u for u, ps in pos.items() if F.near_window(ps, lexs, 10 ** 6, words=nwords)}
        seen["queries"] += 1
        seen["true"] += len(want)
        seen["false"] += len(docs) - len(want)
        seen["all_keys_far"] += len(wide - want)
    # проверка содержательна: есть и попадания, и промахи, и «все слова, но далеко»
    assert seen["queries"] > 200 and seen["true"] > 1000 and seen["all_keys_far"] > 1000, seen


def test_prefix_and_exact_rule(pg, monkeypatch):
    docs = {"cart": "Картридж сломался", "card": "Карта не работает",
            "refuse": "Банк отказал в кредите", "atm": "Банкомат съел карточку"}
    _put(docs)

    def near(key, pmin=5):
        return {u for (u,) in _sql("SELECT url FROM review_index WHERE al_near(tsv, :k, 0, :p)",
                                   {"k": [key], "p": pmin})}
    assert near("карт") == {"card"}                # 4 буквы — только целиком: не «картридж»
    assert near("отказ") == {"refuse"}             # 5 букв — начало слова: «отказа»
    assert near("отказ", pmin=6) == set()          # порог из REVIEWS_NEAR_PREFIX_MIN
    assert near("банк") == {"refuse"}              # не «банкомат»
    assert near("карточк") == {"atm"}
    # тот же порог в Python даёт тот же ответ
    monkeypatch.setattr(F, "_PREFIX_MIN", 6)
    tsv = dict(_sql("SELECT url, tsv::text FROM review_index"))
    assert not F.near_window(F._positions(tsv["refuse"]), ["отказ"])
    # пустые ключи, NULL и вектор без позиций — «нет», не ошибка
    got = _sql("""SELECT al_near(to_tsvector('russian', 'карта'), '{}', 3),
                         al_near(NULL, ARRAY['карт'], 3),
                         al_near(strip(to_tsvector('russian', 'карта')), ARRAY['карт'], 3),
                         al_near(to_tsvector('russian', 'карта'), ARRAY['', 'карт', 'карт'], 0)""")
    assert got[0] == (False, False, False, True)


DOCS = {
    "u1": "Мне навязали страховку при оформлении кредита, отказаться не дали.",
    "u2": "Страховку не навязывали, но кредит одобрили с опозданием на неделю.",
    "u3": "Запретили переводы самому себе между своими счетами в другой банк.",
    "u4": "Переводы себе самому в другой банк больше не проходят.",
    "u5": "Выручку по эквайрингу не зачисляют третий день, деньги покупателей зависли.",
    "u6": "Банк зачислил выручку с опозданием, а эквайринг работает с перебоями.",
    "u7": "Страховка жизни навязана сотрудником, сумма кредита выросла.",
    "u8": "Банк отказал в реструктуризации ипотеки, хотя доход упал вдвое.",
}


def _urls(rows):
    return sorted(r["url"] for r in rows)


def test_spans_and_word_forms_like_search_near(pg):
    _put(DOCS)
    cases = [([["навязали", "страховку"]], None, ["u1", "u7"]),
             ([["не", "зачисляют", "выручку"]], None, ["u5", "u6"]),
             ([["зачисляют", "выручку"]], None, ["u6"]),
             ([["зачисляют", "выручку"]], [3], ["u5", "u6"]),        # служебные слова — позиции
             ([["отказ", "реструктуризации", "ипотеки"]], None, ["u8"]),
             ([["выручка", "банкомат"]], None, [])]
    for groups, spans, want in cases:
        rows = F.search_near_sql(groups, spans=spans)
        assert _urls(rows) == want, groups
        assert F.search_near_sql(groups, spans=spans, count=True) == len(want)
        old, capped = F.search_near(groups, spans=spans)
        assert not capped and _urls(old) == want
        assert {r["url"]: (r["rank"], r["terms"]) for r in rows} == \
            {r["url"]: (r["rank"], r["terms"]) for r in old}
    # группа из одних стоп-слов ничего не ищет и не мешает остальным
    assert F.search_near_sql([["не", "без"]]) == [] and F.search_near_sql([], count=True) == 0
    assert _urls(F.search_near_sql([["не", "без"], ["навязали", "страховку"]])) == ["u1", "u7"]


def test_groups_or_agree_with_search_near(pg):
    rng = random.Random(7)
    _put(_random_docs(rng, 400))
    for _ in range(60):
        groups = [[rng.choice(VOCAB) for _ in range(rng.randint(1, 3))]
                  for _ in range(rng.randint(1, 3))]
        spans = [len(groups[0]) + rng.choice([0, 2])]
        old, capped = F.search_near(groups, spans=spans)
        new = F.search_near_sql(groups, spans=spans)
        assert not capped
        # ранг и terms — от первой подошедшей группы, как в search_near
        assert {r["url"]: (r["rank"], r["terms"]) for r in new} == \
            {r["url"]: (r["rank"], r["terms"]) for r in old}, groups
        assert F.search_near_sql(groups, spans=spans, count=True) == len(new)


def test_fast_paths_equal_al_near(pg):
    """Одна и две основы считаются самим tsquery (две — фразами «a <d> b»), три и больше —
    al_near. Быстрые пути обязаны давать ровно её ответ и ответ search_near — при любом
    разрыве, длине запроса и словоформах (две основы одного слова, основа как начало другой)."""
    rng = random.Random(13)
    _put(_random_docs(rng, 400))
    kinds = {1: 0, 2: 0, 3: 0}
    for _ in range(150):
        words = [rng.choice(VOCAB) for _ in range(rng.randint(1, 3))]
        lexs = _lexs(words)
        if not lexs:
            continue
        gap, nwords = rng.randint(0, 4), len(words) + rng.choice([0, 1, 3])
        keys, span = _args(lexs, gap, nwords)
        kinds[min(len(keys), 3)] += 1
        got = _urls(F.search_near_sql([words], spans=[nwords], gap=gap))
        assert got == _urls(F.search_near([words], spans=[nwords], gap=gap)[0]), (words, gap)
        assert got == sorted(u for (u,) in _sql("SELECT url FROM review_index"
                                                " WHERE al_near(tsv, :k, :s)",
                                                {"k": keys, "s": span})), words
    assert min(kinds.values()) > 10, kinds


def test_count_paging_and_order(pg):
    rng = random.Random(11)
    _put(_random_docs(rng, 400))
    groups = [["кредит"], ["страховку", "навязали"]]
    rows = F.search_near_sql(groups)
    assert len(rows) > 50 and F.search_near_sql(groups, count=True) == len(rows)
    key = [(-r["rank"], -_dt.date.fromisoformat(r["date"]).toordinal(), r["url"]) for r in rows]
    assert key == sorted(key)                                     # rank убыв., дата убыв.
    pages = [F.search_near_sql(groups, limit=17, offset=o) for o in range(0, len(rows) + 17, 17)]
    assert [r["url"] for pg_ in pages for r in pg_] == [r["url"] for r in rows]
    assert F.search_near_sql(groups, limit=5, offset=len(rows)) == []
    # счёт тем же проходом, что и страница
    for off, n in ((0, 10), (30, 10), (len(rows) + 5, 10)):
        meta: dict = {}
        page = F.search_near_sql(groups, limit=n, offset=off, _meta=meta)
        assert meta["total"] == len(rows) and [r["url"] for r in page] == \
            [r["url"] for r in rows[off:off + n]]
    meta = {}
    assert F.search_near_sql([["не", "без"]], _meta=meta) == [] and meta == {"total": 0}


def test_filters_and_extra_sql_before_limit(pg):
    rng = random.Random(5)
    _put(_random_docs(rng, 400))
    groups = [["кредит", "карта"], ["навязали", "страховку", "кредита"]]
    every = F.search_near_sql(groups)
    cards = [r for r in every if r["product"] == "Карты"]
    assert cards and len(cards) < len(every)
    # условие вкладки отбирает ДО страницы: 5 строк продукта, а не 5 лучших минус чужие
    got = F.search_near_sql(groups, extra_sql="f.product = :prod", params={"prod": "Карты"},
                            limit=5)
    assert [r["url"] for r in got] == [r["url"] for r in cards[:5]]
    assert F.search_near_sql(groups, extra_sql="f.product = :prod", params={"prod": "Карты"},
                             count=True) == len(cards)
    # фильтры search_near работают так же
    bank = F.search_near_sql(groups, bank="Банк А", month="2026-03")
    assert bank and {(r["bank"], r["date"][:7]) for r in bank} == {("Банк А", "2026-03")}
    assert _urls(bank) == _urls(F.search_near(groups, bank="Банк А", month="2026-03")[0])
    with pytest.raises(ValueError):
        F.search_near_sql(groups, extra_sql="f.bank = :bank", params={"bank": "x"}, bank="Банк А")
    with pytest.raises(ValueError):
        F.search_near_sql(groups, extra_sql="true", params={"_nq0": "x"})


def test_no_cap(pg):
    """7 000 отзывов со словами рядом и 3 000 — с теми же словами, но далеко: search_near
    упирается в NEAR_CAP, SQL-путь считает всех и только их."""
    _sql("""INSERT INTO review_index (url, review_id, source, bank, dt, tsv)
            SELECT 'n' || g, g, 'bankiru', 'Банк А',
                   timestamp '2026-01-01' + g * interval '1 minute', t.v
              FROM generate_series(1, 7000) g,
                   (SELECT to_tsvector('russian',
                                       'Мне навязали страховку при оформлении кредита') v) t""")
    _sql("""INSERT INTO review_index (url, review_id, source, bank, dt, tsv)
            SELECT 'f' || g, 10000 + g, 'bankiru', 'Банк А', timestamp '2026-01-01', t.v
              FROM generate_series(1, 3000) g,
                   (SELECT to_tsvector('russian', 'Навязали услугу, а через месяц пришёл счёт и
                           отдельно ещё страховку') v) t""")
    groups = [["навязали", "страховку"]]
    assert F.search_near_sql(groups, count=True) == 7000
    old, capped = F.search_near(groups)
    assert capped and len(old) <= F.NEAR_CAP < 7000
    rows = F.search_near_sql(groups)
    assert len(rows) == 7000 and all(r["url"].startswith("n") for r in rows)
    assert rows[0]["url"] == "n7000"                              # ранги равны — свежие выше
    tail = F.search_near_sql(groups, limit=50, offset=6990)
    assert [r["url"] for r in tail] == [f"n{k}" for k in range(10, 0, -1)]
    # без окна (одни слова где угодно) было бы 10 000 — al_near действительно фильтрует
    assert _sql("""SELECT count(*) FROM review_index
                    WHERE tsv @@ to_tsquery('simple', 'навяза:* & страхо:*')""")[0][0] == 10000
    # одно слово — без вызова al_near, но тем же счётом, что дала бы она
    assert F.search_near_sql([["страховку"]], count=True) == 10000 == \
        _sql("SELECT count(*) FROM review_index WHERE al_near(tsv, ARRAY['страхо'], 0)")[0][0]
    assert len(F.search_near_sql([["страховку"]], limit=None)) == 10000


def test_ready_and_failure_is_raised(pg, monkeypatch):
    assert F.near_sql_ready()

    class Boom:
        def __enter__(self): raise RuntimeError("нет базы")
        def __exit__(self, *a): return False
    monkeypatch.setattr(F.db, "session", lambda: Boom())
    # сбой не маскируется под «ничего не нашлось»
    with pytest.raises(RuntimeError):
        F.search_near_sql([["перевод"]])
    with pytest.raises(RuntimeError):
        F.search_near_sql([["перевод"]], count=True)


def test_benchmark_20k(pg):
    """Скорость al_near на 20 тыс. строк (один процесс, без параллельных воркеров) и для
    сравнения — прежний путь: tsv::text в Python, разбор позиций и near_window."""
    big = VOCAB + [f"слово{k}" for k in range(400)]         # богаче словарь — длиннее векторы
    # тексты собирает сама база: 20 тыс. строк через драйвер — это замер драйвера
    _sql("""INSERT INTO review_index (url, review_id, source, bank, dt, tsv)
            SELECT 'b' || g, g, 'bankiru', 'Банк А', timestamp '2026-01-01',
                   to_tsvector('russian', (
                   SELECT string_agg(w.v[1 + floor(random() * cardinality(w.v) + s * 0)::int], ' ')
                     FROM generate_series(1, 40 + g % 120) s))
              FROM generate_series(1, 20000) g, (SELECT CAST(:v AS text[]) AS v) w""", {"v": big})
    lexs = _lexs(["зачисляют", "выручку", "эквайринг"])
    keys, span = _args(lexs, F.NEAR_GAP, 3)
    from sqlalchemy import text
    with db.session() as s:
        s.execute(text("SET max_parallel_workers_per_gather = 0"))
        s.execute(text("SELECT count(*) FROM review_index")).scalar()   # прогрев кеша
        t0 = time.perf_counter()
        n = s.execute(text("SELECT count(*) FILTER (WHERE al_near(tsv, :k, :s)) FROM review_index"),
                      {"k": keys, "s": span}).scalar()
        dt_sql = time.perf_counter() - t0
        t0 = time.perf_counter()
        tsvs = s.execute(text("SELECT tsv::text FROM review_index")).scalars().all()
        n_py = sum(F.near_window(F._positions(t), lexs, F.NEAR_GAP, words=3) for t in tsvs)
        dt_py = time.perf_counter() - t0
        avg = s.execute(text("SELECT avg(length(tsv)) FROM review_index")).scalar()
    assert n == n_py
    rate, rate_py = 20000 / dt_sql, 20000 / dt_py
    print(f"\nal_near: 20000 строк за {dt_sql:.3f} с — {rate:,.0f} строк/с "
          f"(в среднем {float(avg):.0f} лексем; попаданий {n}); "
          f"Python tsv::text + near_window: {dt_py:.3f} с — {rate_py:,.0f} строк/с")
    assert rate > 2000                                       # грубая страховка от деградации
