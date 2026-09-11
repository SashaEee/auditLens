"""Смысловой поиск на витрине «Рынок»: контракт запроса и устойчивость.

Замечания аудиторов (8 штук одним корнем): поиск шёл по формам слов —
«детская карта» находилась у трёх банков и не находилась у четвёртого,
«выдача дебетовой карты» распадалась на отдельные слова.
"""
import inspect
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

ok = fail = 0


def check(name, cond):
    global ok, fail
    ok, fail = ok + bool(cond), fail + (not cond)
    print(("  ✓ " if cond else "  ✗ ") + name)


APP = (pathlib.Path(__file__).resolve().parents[1]
       / "src/bank_audit/web/app.py").read_text(encoding="utf-8")
MIG = (pathlib.Path(__file__).resolve().parents[1]
       / "migrations/067_offer_search_vector.sql").read_text(encoding="utf-8")
src = APP[APP.index("def _market_rows("):APP.index("def market_export(")]

print("— три ноги поиска —")
check("подстрока осталась", "m.bank_name ILIKE :qq" in src and "m.title ILIKE :qq" in src)
check("полнотекст остался", "websearch_to_tsquery" in src)
check("добавлена смысловая нога", "SELECT offer_id FROM near" in src)
check("ноги объединяются, а не заменяют друг друга", '" OR ".join(legs)' in src)
check("вектор считается один раз на запрос", src.count("_market_query_vector(q_text)") == 1)

print("\n— устойчивость —")
check("без вектора поиск работает как раньше", "if qvec:" in src)
vec_fn = APP[APP.index("def _market_query_vector("):APP.index("def _market_rows(")]
check("эмбеддер не роняет поиск", "except Exception" in vec_fn and "return None" in vec_fn)
check("на коротком запросе вектор не считается", "len(text_q) < 3" in vec_fn)
check("ветка near появляется только вместе с вектором", 'if "qvec" in params:' in src)
check("пул ближайших ограничен", "_MARKET_VEC_POOL" in APP and "LIMIT :near_pool" in src)
check("порога по косинусу нет", "<=>" in src and "cosine" not in src.lower())

print("\n— порядок выдачи не меняется —")
order = src[src.index("ORDER BY lower(regexp_replace(m.bank_name"):]
check("сортировка по банку и названию сохранена", "lower(m.title)" in order)
check("вектор не участвует в сортировке", "<=>" not in order.split("LIMIT :l")[0])

print("\n— таблица и миграция —")
check("джойн к таблице предложений добавлен", "LEFT JOIN product_offer o ON o.offer_id = m.offer_id" in src)
check("миграция добавляет вектор и полнотекст", "ADD COLUMN IF NOT EXISTS embedding vector(1024)" in MIG
      and "search_tsv tsvector" in MIG)
check("миграция идемпотентна", MIG.count("IF NOT EXISTS") >= 5)
check("индекс полнотекста создаётся", "USING GIN (search_tsv)" in MIG)

print("\n— скрипт бэкфилла —")
BF = (pathlib.Path(__file__).resolve().parents[1]
      / "scripts/backfill_offer_vectors.py").read_text(encoding="utf-8")
check("в текст входят банк, название, вид и условия",
      all(w in BF for w in ("bank_name", "title", "category", "conditions")))
check("считает только недостающее по умолчанию", "o.embedding IS NULL" in BF)
check("полнотекст пишется тем же текстом", "search_tsv = to_tsvector" in BF)

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
