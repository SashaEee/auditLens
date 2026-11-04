"""Склейка копий одного отзыва между площадками (rag/review_dedup.py): база — SQLite,
сеть и соседняя база banki.ru не нужны."""
from __future__ import annotations

import os
import random
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.rag import review_dedup as RD  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 20, 12, 0)

STORY = (
    "Добрый день. 12 сентября я оплатил картой покупку в магазине электроники на сумму "
    "48 990 рублей. По условиям акции за эту покупку мне полагался повышенный кэшбэк, "
    "категорию я выбрал заранее в приложении. Прошёл месяц, кэшбэк так и не начислен. "
    "Я трижды писал в чат поддержки, каждый раз оператор просил подождать ещё пять рабочих "
    "дней и закрывал обращение. На горячей линии мне сказали, что магазин относится к другой "
    "категории, хотя в чеке и в выписке указан правильный код торговой точки. Считаю, что банк "
    "просто не выполняет собственные условия программы лояльности и вводит клиентов в "
    "заблуждение. Прошу провести проверку, начислить положенные бонусы и дать письменный ответ "
    "по существу, а не очередную отписку. Если ответа не будет, буду обращаться в Центральный банк."
)
# Та же история на другой площадке: без приветствия, другой регистр и пунктуация,
# «е» вместо «ё», сумма без пробела, «трижды» → «три раза»
STORY_COPY = (
    "12 сентября я оплатил картой покупку в магазине электроники на сумму 48990 рублей. "
    "По условиям акции за эту покупку мне полагался повышенный КЭШБЭК, категорию я выбрал "
    "заранее в приложении. Прошел месяц, кэшбэк так и не начислен!!! Я три раза писал в чат "
    "поддержки - каждый раз оператор просил подождать еще пять рабочих дней и закрывал "
    "обращение. На горячей линии мне сказали, что магазин относится к другой категории, хотя "
    "в чеке и в выписке указан правильный код торговой точки. Считаю, что банк просто не "
    "выполняет собственные условия программы лояльности и вводит клиентов в заблуждение.\n\n"
    "Прошу провести проверку, начислить положенные бонусы и дать письменный ответ по существу, "
    "а не очередную отписку. Если ответа не будет, буду обращаться в Центральный банк."
)
# Разные люди, один сюжет «не пришёл кэшбэк» и общая шаблонная фраза
TEMPLATE = ("Кэшбэк так и не пришёл, обратился в чат поддержки, там ответили что нужно "
            "подождать до конца месяца.")
CASHBACK = [
    "Добрый день. Оплатил покупку в супермаркете 3 октября на 2 340 рублей, по акции должен "
    f"был прийти кэшбэк 10 процентов. {TEMPLATE} Месяц прошёл, денег нет, на повторные "
    "обращения отвечают шаблонными фразами и закрывают заявку. Прошу разобраться и начислить "
    "кэшбэк.",
    "Добрый день. В августе подключил категорию повышенного кэшбэка на АЗС и заправлялся "
    f"только этой картой, потратил около 8 тысяч. {TEMPLATE} Сейчас уже середина октября, "
    "бонусов нет, оператор говорит что код торговой точки не подходит. Прошу разобраться и "
    "начислить кэшбэк.",
    "Добрый день. Оформил кредитную карту ради кэшбэка на рестораны, в сентябре потратил в "
    f"кафе 15 000 рублей. {TEMPLATE} В итоге начислили только за одну операцию, по остальным "
    "отказали без объяснений. Прошу разобраться и начислить кэшбэк.",
    "Здравствуйте. Купил билеты на поезд через приложение банка, обещали кэшбэк милями. "
    f"{TEMPLATE} Мили так и не появились, в истории операций покупка есть, а начисления нет. "
    "Прошу разобраться и начислить кэшбэк.",
    "Здравствуйте. Подписка на сервис обещала кэшбэк 30 процентов в аптеках, я купил "
    f"лекарства на 6 тысяч. {TEMPLATE} Через месяц пришло уведомление, что аптека не "
    "участвует в программе, хотя в списке партнёров она была. Прошу разобраться и начислить "
    "кэшбэк.",
]


def row(url, text, *, bank="Сбербанк", source="sravni_reviews", dt=NOW, **kw):
    return {"url": url, "text": text, "bank": bank, "source": source, "dt": dt, **kw}


# ── Текст → отпечаток ────────────────────────────────────────────────────────

def test_tokens_case_yo_punctuation_numbers():
    assert RD.tokens("Ёлка, ПРИШЁЛ!!! 5 000 руб. 12.03.2025 в 10:30 — «Кэшбэк» 5000р") == \
        ["елка", "пришел", "#", "руб", "#", "в", "#", "кэшбэк", "#", "р"]
    assert RD.tokens(None) == [] and RD.tokens("") == []


def test_minor_edits_keep_jaccard_above_threshold():
    a, b = RD.fingerprint(STORY), RD.fingerprint(STORY_COPY)
    j = RD.jaccard(a, b)
    assert 0.8 <= j < 1.0                                   # правки есть, но мелкие
    assert abs(RD.jaccard_estimate(a, b) - j) < 0.15        # оценка MinHash рядом с точным
    assert RD.jaccard(a, a) == 1.0 and RD.jaccard_estimate(a, a) == 1.0
    # регистр, «ё» и пробелы у пунктуации шинглов не меняют
    same = STORY.upper().replace("Ё", "Е").replace(",", " ,")
    assert RD.jaccard(a, RD.fingerprint(same)) == 1.0


def test_short_text_has_no_fingerprint():
    assert RD.fingerprint("Ужасный банк, не рекомендую никому, деньги списали без спроса") is None
    assert RD.fingerprint(None) is None


def test_lsh_bands_catch_pairs_on_threshold():
    for t in (0.5, 0.7, 0.8, 0.9, 1.0):
        b, r = RD.lsh_params(t)
        assert b * r <= RD.NUM_PERM
        assert 1 - (1 - t ** r) ** b >= RD.TARGET_RECALL, t
    assert RD.lsh_params(0.8) == (25, 5)
    with pytest.raises(ValueError):
        RD.lsh_params(0)


def test_fingerprint_is_deterministic_across_processes():
    """Подпись не зависит от процесса и порядка вызовов: соли — blake2b, не hash().
    Числа — эталон; поменялись — значит, поменялась схема хэшей, и все прошлые
    отметки копий надо пересчитать полным проходом."""
    fp = RD.fingerprint("раз два три четыре пять шесть семь восемь девять десять "
                        "одиннадцать двенадцать тринадцать четырнадцать пятнадцать")
    assert fp.size == 11
    assert fp.sig[:4].tolist() == [3086951584, 3532779093, 2544392784, 16672893]
    assert fp.sh[:3].tolist() == [830917937, 1574987776, 2044310117]


# ── Правила склейки ──────────────────────────────────────────────────────────

def test_copy_on_other_platform_is_glued_to_bankiru():
    rows = [row("s", STORY_COPY, source="sravni_reviews", dt=NOW - timedelta(days=3)),
            row("k", STORY, source="bankiru", dt=NOW),
            row("f", STORY, source="finuslugi_reviews", dt=NOW - timedelta(days=1))]
    # каноническая — копия banki.ru, хотя она самая поздняя
    assert RD.find_dups(rows) == {"s": "k", "f": "k"}


def test_without_bankiru_the_earliest_is_canonical():
    rows = [row("b", STORY, source="finuslugi_reviews", dt=NOW - timedelta(days=1)),
            row("a", STORY_COPY, source="sravni_reviews", dt=NOW - timedelta(days=2)),
            row("c", STORY, source="banki_reviews", dt=NOW)]
    assert RD.find_dups(rows) == {"b": "a", "c": "a"}
    # одно и то же время — решает адрес, а не порядок строк
    same = [row("z", STORY, dt=NOW), row("y", STORY, dt=NOW)]
    assert RD.find_dups(same) == {"z": "y"} == RD.find_dups(same[::-1])


def test_same_template_different_stories_are_not_glued():
    fps = [RD.fingerprint(t) for t in CASHBACK]
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            assert RD.jaccard(fps[i], fps[j]) < 0.4, (i, j)
    rows = [row(f"u{k}", t, dt=NOW - timedelta(days=k)) for k, t in enumerate(CASHBACK)]
    assert RD.find_dups(rows) == {}
    assert RD.find_dups(rows, threshold=0.5) == {}


def test_other_bank_is_not_glued():
    rows = [row("a", STORY, bank="Сбербанк", source="bankiru"),
            row("b", STORY, bank="ВТБ", source="sravni_reviews")]
    assert RD.find_dups(rows) == {}


def test_window_by_calendar_days():
    base = datetime(2026, 10, 1, 0, 5)

    def pair(later):
        return [row("a", STORY, dt=base), row("b", STORY, dt=later)]
    # 14 календарных дней — в окне, даже если часов между отзывами больше 14 суток
    assert RD.find_dups(pair(base + timedelta(days=14, hours=23))) == {"b": "a"}
    assert RD.find_dups(pair(base + timedelta(days=15))) == {}
    assert RD.find_dups(pair(base + timedelta(days=15)), window_days=15) == {"b": "a"}
    # строки без даты окно не проверить — не склеиваем
    assert RD.find_dups([row("a", STORY, dt=None), row("b", STORY)]) == {}


def test_copy_is_compared_with_canonical_directly():
    """A (1-е) ~ B (11-е) ~ C (21-е): C — не копия A, между ними 20 дней. Цепочка
    сходства не склеивает далёкие строки."""
    d0 = datetime(2026, 9, 1, 10)
    rows = [row("a", STORY, dt=d0), row("b", STORY, dt=d0 + timedelta(days=10)),
            row("c", STORY, dt=d0 + timedelta(days=20))]
    assert RD.find_dups(rows) == {"b": "a"}


def test_short_texts_are_never_glued():
    t = "Карту заблокировали без объяснений, поддержка молчит третий день"
    assert RD.find_dups([row("a", t), row("b", t, source="bankiru")]) == {}


def test_cross_source_only():
    rows = [row("a", STORY, source="bankiru", dt=NOW - timedelta(days=1)),
            row("b", STORY, source="bankiru", dt=NOW)]
    assert RD.find_dups(rows) == {"b": "a"}                 # повтор на той же площадке — тоже копия
    assert RD.find_dups(rows, cross_source_only=True) == {}


def test_result_does_not_depend_on_input_order():
    rows = [row("k", STORY, source="bankiru", dt=NOW),
            row("s", STORY_COPY, dt=NOW - timedelta(days=2))]
    rows += [row(f"c{k}", t, dt=NOW - timedelta(days=k)) for k, t in enumerate(CASHBACK)]
    rows += [row("v", STORY, bank="ВТБ", source="finuslugi_reviews")]
    want = RD.find_dups(rows)
    assert want == {"s": "k"}
    for seed in range(5):
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        assert RD.find_dups(shuffled) == want


def test_locked_rows_keep_decisions_and_take_new_copies():
    """Закреплённые — решения прошлых проходов: сами не помечаются, а новая копия идёт
    к закреплённой канонической (даже если сама из banki.ru) или к канонической
    закреплённой копии."""
    old = NOW - timedelta(days=5)
    rows = [row("old", STORY, source="sravni_reviews", dt=old, locked=True),
            row("new", STORY_COPY, source="bankiru", dt=NOW)]
    assert RD.find_dups(rows) == {"new": "old"}
    rows = [row("old_copy", STORY, dt=old, locked=True, dup_of="root-outside"),
            row("new", STORY_COPY, source="finuslugi_reviews", dt=NOW)]
    assert RD.find_dups(rows) == {"new": "root-outside"}
    # закреплённая копия самой строки не делает строку копией себя
    rows = [row("old_copy", STORY, dt=old, locked=True, dup_of="k"),
            row("k", STORY, source="bankiru", dt=NOW)]
    assert RD.find_dups(rows) == {}
    # две закреплённые не трогаются, даже если одинаковы
    rows = [row("a", STORY, dt=old, locked=True), row("b", STORY, dt=old, locked=True)]
    assert RD.find_dups(rows) == {}


def test_decide_leads_copies_to_root():
    """Новая u похожа на закреплённую копию L (её каноническая — K), а K на шаге 2
    сама стала копией более ранней C из banki.ru: u ведёт к C, а не к K —
    dup_of всегда указывает на строку с пустой dup_of."""
    def doc(url, source, days, locked=False, cur=None):
        dt = NOW - timedelta(days=days)
        return RD._Doc(url=url, bank="Сбербанк", source=source, ts=dt.timestamp(),
                       day=dt.toordinal(), fp=None, locked=locked, cur=cur)
    docs = [doc("L", "sravni_reviews", 20, locked=True, cur="K"), doc("K", "bankiru", 3),
            doc("C", "bankiru", 6), doc("u", "finuslugi_reviews", 1)]
    edges = {3: {0: 0.9}, 0: {3: 0.9}, 1: {2: 0.95}, 2: {1: 0.95}}
    out = RD._decide(docs, edges)
    assert {docs[k].url: c for k, (c, _j) in out.items()} == {"K": "C", "u": "C"}


def test_dates_of_any_shape():
    assert RD._as_dt("2026-10-01 10:00:00") == datetime(2026, 10, 1, 10)
    assert RD._as_dt("2026-10-01T10:00:00Z") == datetime(2026, 10, 1, 10)
    assert RD._as_dt(datetime(2026, 10, 1).date()) == datetime(2026, 10, 1)
    assert RD._as_dt("не дата") is None and RD._as_dt(None) is None


# ── Бандинг не теряет пар: сравнение с полным перебором ──────────────────────

_SYL = ["ба", "ве", "ги", "до", "жу", "за", "ки", "ло", "ми", "но", "пу", "ра", "си", "ту"]
_BANKS = ["Банк А", "Банк Б"]
_SOURCES = ["bankiru", "sravni_reviews", "finuslugi_reviews"]


def _corpus(seed: int = 7, stories: int = 60) -> list[dict]:
    """Синтетика: истории из случайных «слов» с общей шаблонной вставкой, у каждой
    1–4 копии с 0–5 заменёнными словами — от точных копий до пар у самого порога."""
    rnd = random.Random(seed)
    vocab = sorted({"".join(rnd.choice(_SYL) for _ in range(rnd.randint(2, 4)))
                    for _ in range(900)})
    common = [rnd.choice(vocab) for _ in range(12)]
    t0 = datetime(2026, 8, 1, 10)
    rows = []
    for s in range(stories):
        words = [rnd.choice(vocab) for _ in range(rnd.randint(30, 110))]
        at = rnd.randrange(len(words))
        words[at:at] = common
        bank = rnd.choice(_BANKS)
        day = rnd.randint(0, 40)
        for v in range(rnd.randint(1, 4)):
            w = list(words)
            for _ in range(rnd.choice([0, 0, 1, 2, 3, 5])):
                w[rnd.randrange(len(w))] = rnd.choice(vocab)
            dt = t0 + timedelta(days=day + rnd.randint(-2, 18), hours=rnd.randint(0, 23))
            rows.append(row(f"https://example.test/{s}/{v}", " ".join(w),
                            bank=bank if rnd.random() > 0.1 else rnd.choice(_BANKS),
                            source=rnd.choice(_SOURCES), dt=dt))
    return rows


def _brute_force(rows: list[dict], window_days: int = 14, threshold: float = 0.8):
    docs = [RD._doc(r, RD.fingerprint(r["text"])) for r in rows]
    edges: dict[int, dict[int, float]] = defaultdict(dict)
    near = 0
    for i in range(len(docs)):
        for j in range(i + 1, len(docs)):
            a, b = docs[i], docs[j]
            if a.fp is None or b.fp is None or a.bank != b.bank:
                continue
            if abs(a.day - b.day) > window_days:
                continue
            jv = RD.jaccard(a.fp, b.fp)
            near += 0.7 <= jv < 0.9
            if jv >= threshold:
                edges[i][j] = edges[j][i] = jv
    return {docs[k].url: c for k, (c, _j) in RD._decide(docs, edges).items()}, near


def test_banding_finds_what_brute_force_finds():
    rows = _corpus()
    want, near = _brute_force(rows)
    assert len(want) >= 20 and near >= 10                   # есть и копии, и пары у порога
    assert RD.find_dups(rows) == want
    want9, _ = _brute_force(rows, threshold=0.9)
    assert RD.find_dups(rows, threshold=0.9) == want9 != want


# ── Пакетный проход по индексу (SQLite) ──────────────────────────────────────

_DDL = (
    """CREATE TABLE review_index (url TEXT PRIMARY KEY, review_id INTEGER NOT NULL,
        source TEXT NOT NULL DEFAULT 'bankiru', bank TEXT NOT NULL, dt TIMESTAMP,
        dup_of TEXT)""",
    "CREATE TABLE review_index_state (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at TIMESTAMP)",
)
_INS = ("INSERT INTO review_index (url, review_id, source, bank, dt, dup_of) "
        "VALUES (:u, :i, :s, :b, :dt, :d)")


@pytest.fixture
def lite(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from bank_audit import db
    eng = create_engine(f"sqlite:///{tmp_path / 'ri.db'}", future=True)
    with eng.begin() as c:
        for ddl in _DDL:
            c.execute(text(ddl))
    monkeypatch.setattr(db, "_Session",
                        sessionmaker(bind=eng, expire_on_commit=False, future=True))
    texts: dict[str, str] = {}
    calls: list[int] = []

    def bodies(rows):
        calls.append(len(rows))
        return {r["url"]: {"text": texts[r["url"]]} for r in rows if r["url"] in texts}
    monkeypatch.setattr(RD, "_bodies", bodies)

    class Lite:
        bodies_calls = calls

        def add(self, url, text_, *, bank="Сбербанк", source="sravni_reviews", days_ago=1.0,
                dup_of=None):
            if text_ is not None:
                texts[url] = text_
            dt = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
            with eng.begin() as c:
                c.execute(text(_INS), {"u": url, "i": len(texts) + 1000, "s": source,
                                       "b": bank, "d": dup_of, "dt": dt})

        def set_text(self, url, text_):
            texts[url] = text_

        def drop_text(self, url):
            texts.pop(url, None)

        def dup_of(self) -> dict:
            with eng.connect() as c:
                return dict(c.execute(text("SELECT url, dup_of FROM review_index")).all())

    yield Lite()
    eng.dispose()


def test_run_marks_copies_and_is_idempotent(lite):
    lite.add("k", STORY, source="bankiru", days_ago=2)
    lite.add("s", STORY_COPY, source="sravni_reviews", days_ago=4)
    lite.add("f", STORY, source="finuslugi_reviews", days_ago=3)
    lite.add("vtb", STORY, bank="ВТБ", days_ago=3)
    for k, t in enumerate(CASHBACK):
        lite.add(f"c{k}", t, days_ago=k + 1)
    r = RD.run(30, now=NOW)
    assert r["dups"] == 2 and r["changed"] == 2 and r["written"] == 2 and r["failed"] == 0
    assert r["pairs"] == {"sravni_reviews→bankiru": 1, "finuslugi_reviews→bankiru": 1}
    got = lite.dup_of()
    assert got["s"] == "k" and got["f"] == "k"
    assert all(got[u] is None for u in ("k", "vtb", "c0", "c1", "c2", "c3", "c4"))
    # повторный проход по тем же данным ничего не пишет
    r2 = RD.run(30, now=NOW)
    assert r2["changed"] == 0 and r2["written"] == 0 and r2["dups"] == 2
    assert lite.dup_of() == got
    # копию переписали в другую историю — отметка снимается
    lite.set_text("s", "Открыл вклад в отделении, через неделю ставку снизили без уведомления, "
                       "менеджер сказал, что так написано в договоре мелким шрифтом на последней "
                       "странице.")
    r3 = RD.run(30, now=NOW)
    assert r3["changed"] == 1 and lite.dup_of()["s"] is None and lite.dup_of()["f"] == "k"


def test_run_small_batches(lite):
    lite.add("k", STORY, source="bankiru", days_ago=2)
    for k in range(5):
        lite.add(f"s{k}", STORY_COPY, source="sravni_reviews", days_ago=3 + k)
    # пять копий с одной площадки склеиваются только без ограничения «одна копия на
    # площадку» — здесь проверяется запись пакетами, поэтому режим без ограничения
    r = RD.run(30, now=NOW, chunk=2, cross_source_only=False)
    assert r["written"] == 5 and set(lite.bodies_calls) <= {1, 2}
    assert all(v == "k" for u, v in lite.dup_of().items() if u != "k")


def test_run_keeps_context_rows_and_attaches_new_copies(lite):
    # раньше окна пересмотра (30 дней) — контекст: решения не меняются,
    # даже не отмеченная прошлым проходом копия old_b остаётся как есть
    lite.add("old_a", STORY, source="bankiru", days_ago=33)
    lite.add("old_b", STORY, source="sravni_reviews", days_ago=34)
    # новая копия в окне пересмотра идёт к контекстной канонической
    lite.add("new", STORY_COPY, source="finuslugi_reviews", days_ago=29)
    # совсем старое (раньше контекста) не читается вовсе
    lite.add("ancient", STORY, source="sravni_reviews", days_ago=200)
    r = RD.run(30, now=NOW)
    assert lite.dup_of() == {"ancient": None, "new": "old_a", "old_a": None, "old_b": None}
    assert r["revisable"] == 1 and r["rows"] == 3


def test_run_new_copy_of_old_copy_goes_to_its_canonical(lite):
    lite.add("root", CASHBACK[1], source="bankiru", days_ago=60)          # вне чтения
    lite.add("old_copy", STORY, source="sravni_reviews", days_ago=35, dup_of="root")
    lite.add("new", STORY_COPY, source="finuslugi_reviews", days_ago=25)
    RD.run(30, now=NOW)
    assert lite.dup_of()["new"] == "root" and lite.dup_of()["old_copy"] == "root"


def test_run_freezes_rows_without_texts(lite):
    """Тексты внешнего корпуса не забрались: строка banki.ru и её копии остаются как
    были — иначе копия «потеряла» бы каноническую и стала второй жалобой."""
    lite.add("k", STORY, source="bankiru", days_ago=2)
    lite.add("s", STORY_COPY, source="sravni_reviews", days_ago=3)
    for k, t in enumerate(CASHBACK):
        lite.add(f"c{k}", t, days_ago=k + 1)
    for k in range(20):
        lite.add(f"x{k}", CASHBACK[k % 5] + f" Номер обращения {k}, филиал {k * 7}.",
                 bank="Банк Икс")
    RD.run(30, now=NOW)
    assert lite.dup_of()["s"] == "k"
    lite.drop_text("k")
    r = RD.run(30, now=NOW)
    assert r["missing"] == 1 and r["frozen"] == 2 and r["changed"] == 0
    assert lite.dup_of()["s"] == "k"
    # пропала заметная доля текстов банка — банк не пересматривается вовсе
    for k in range(10):
        lite.drop_text(f"x{k}")
    r = RD.run(30, now=NOW)
    assert "Банк Икс" in r["skipped_banks"] and "Сбербанк" not in r["skipped_banks"]


def test_run_dry_writes_nothing_and_shows_borderline(lite):
    lite.add("k", STORY, source="bankiru", days_ago=2)
    lite.add("s", STORY_COPY, source="sravni_reviews", days_ago=4)
    lite.add("f", STORY, source="finuslugi_reviews", days_ago=3)
    r = RD.run(30, now=NOW, dry=True, examples=1)
    assert r["dry"] and r["changed"] == 2 and r["written"] == 0
    assert all(v is None for v in lite.dup_of().values())
    [ex] = r["examples"]                                     # пограничная пара — первой
    assert ex["url"] == "s" and ex["canon"] == "k" and 0.8 <= ex["jaccard"] < 1
    assert ex["source"] == "sravni_reviews" and ex["canon_source"] == "bankiru"


def test_full_pass_reads_from_since(lite, monkeypatch):
    monkeypatch.setattr(RD, "SINCE", (NOW - timedelta(days=100)).strftime("%Y-%m-%d"))
    lite.add("k", STORY, source="bankiru", days_ago=90)
    lite.add("s", STORY_COPY, source="sravni_reviews", days_ago=95)
    lite.add("too_old", STORY, source="sravni_reviews", days_ago=300)
    r = RD.run(None, now=NOW)
    assert r["mode"] == "full" and lite.dup_of() == {"k": None, "s": "k", "too_old": None}


def test_repair_orphans_chains_and_self_links(lite):
    lite.add("gone_copy", None, dup_of="gone")              # каноническая удалена
    lite.add("self", None, dup_of="self")
    lite.add("a", None, dup_of="b")                          # a → b → c → d
    lite.add("b", None, dup_of="c")
    lite.add("c", None, dup_of="d")
    lite.add("d", None)
    lite.add("x", None, dup_of="y")                          # y вела к удалённой: x → y
    lite.add("y", None, dup_of="vanished")
    assert RD.repair() > 0
    assert lite.dup_of() == {"a": "d", "b": "d", "c": "d", "d": None, "gone_copy": None,
                             "self": None, "x": "y", "y": None}
    assert RD.repair() == 0


def test_write_retries_after_failure(lite, monkeypatch):
    from bank_audit import db
    lite.add("a", None)
    lite.add("b", None)
    real = db.session
    fails = {"n": 1}

    def flaky():
        if fails["n"]:
            fails["n"] -= 1
            raise RuntimeError("deadlock detected")
        return real()
    monkeypatch.setattr(db, "session", flaky)
    monkeypatch.setattr(RD.time, "sleep", lambda s: None)
    assert RD.write([("b", "a")], chunk=1) == (1, 0)
    monkeypatch.setattr(db, "session", real)
    assert lite.dup_of()["b"] == "a"


def test_tick_full_first_then_incremental(lite, monkeypatch):
    seen, answer = [], {"failed": 0, "skipped_banks": {}}

    def fake_run(since_days=None, **kw):
        seen.append(since_days)
        return answer
    monkeypatch.setattr(RD, "run", fake_run)
    t = 1_800_000_000.0
    RD.tick(now=t)                                           # первый запуск — полный проход
    assert RD.tick(now=t + 60) == {"skipped": "not_due"}
    RD.tick(now=t + RD.EVERY_S + 1)                          # дальше — инкремент
    RD.tick(now=t + RD.FULL_EVERY_S + 1)                     # раз в неделю — снова полный
    assert seen == [None, RD.SINCE_DAYS, None]
    # проход не состоялся (занят замок) — срок не сдвигается, следующий тик повторит
    answer = {"skipped": "locked"}
    t2 = t + RD.FULL_EVERY_S + RD.EVERY_S + 2
    RD.tick(now=t2)
    RD.tick(now=t2 + 1)
    assert seen[-2:] == [RD.SINCE_DAYS, RD.SINCE_DAYS]


def test_tick_repeats_incomplete_full_pass_not_too_often(lite, monkeypatch):
    seen, answer = [], {"failed": 0, "skipped_banks": {"Сбербанк": "нет текстов у 900 из 1000"}}

    def fake_run(since_days=None, **kw):
        seen.append(since_days)
        return answer
    monkeypatch.setattr(RD, "run", fake_run)
    t = 1_800_000_000.0
    RD.tick(now=t)                                           # полный, но банк пропущен
    assert RD.tick(now=t + 60) == {"skipped": "not_due"}     # не чаще раза в EVERY_S
    answer = {"failed": 0, "skipped_banks": {}}
    RD.tick(now=t + RD.EVERY_S + 1)                          # полный повторён и прошёл
    RD.tick(now=t + 2 * RD.EVERY_S + 2)                      # дальше — инкремент
    assert seen == [None, None, RD.SINCE_DAYS]


def test_ok_stamp_only_when_most_banks_were_glued(lite, monkeypatch):
    # отметка удачного прохода — для сторожа «Пульса»: проход, пропустивший все банки,
    # её не двигает, хотя dedup_at пишется (ревью 04.11.2026)
    answer = {"failed": 0, "banks": 2, "skipped_banks": {"А": "нет текстов", "Б": "нет текстов"}}
    monkeypatch.setattr(RD, "run", lambda since_days=None, **kw: answer)
    t = 1_800_000_000.0
    RD.tick(now=t)
    st = RD._state_get()
    assert st.get(RD._AT_KEY) == t and RD._OK_KEY not in st
    answer = {"failed": 0, "banks": 3, "skipped_banks": {"А": "нет текстов"}}
    RD.tick(now=t + RD.EVERY_S + 1)
    assert RD._state_get()[RD._OK_KEY] == t + RD.EVERY_S + 1


# ── Миграция ─────────────────────────────────────────────────────────────────

def test_migration_is_safe_for_hot_table():
    sql = (ROOT / "migrations" / "103_review_dup.sql").read_text(encoding="utf-8")
    assert sql.splitlines()[0].strip() == "SET lock_timeout = '3s';"
    assert "ALTER TABLE review_index ADD COLUMN IF NOT EXISTS dup_of text;" in sql
    assert "CREATE INDEX IF NOT EXISTS review_index_dup_of ON review_index (dup_of)" in sql
    assert "%" not in sql                                    # миграции идут через exec_driver_sql


def test_cross_only_takes_one_copy_per_platform():
    """Пять одинаковых отзывов на sravni и один на banki.ru: к канонической уходит одна
    копия — остальные четыре остаются жалобами (разные люди по шаблону)."""
    from datetime import datetime
    text_ = STORY
    rows = [{"url": "b", "bank": "Б", "dt": datetime(2026, 10, 1), "source": "bankiru", "text": text_}]
    rows += [{"url": f"s{i}", "bank": "Б", "dt": datetime(2026, 10, 2 + i), "source": "sravni_reviews", "text": text_}
             for i in range(5)]
    rows += [{"url": "f1", "bank": "Б", "dt": datetime(2026, 10, 3), "source": "finuslugi_reviews", "text": text_}]
    got = RD.find_dups(rows, cross_source_only=True)
    assert sum(1 for k in got if k.startswith("s")) == 1 and got.get("f1") == "b"
    assert set(got.values()) == {"b"}
    assert len(RD.find_dups(rows, cross_source_only=False)) == 6     # без ограничения — все копии
