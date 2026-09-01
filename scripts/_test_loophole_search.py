"""Этап 4: гибридный поиск по «Лазейкам».

Проверяем то, что можно проверить без Postgres: форму запроса, разметку
происхождения совпадения и запасные пути. Живой замер релевантности — в
scripts/probe_search.py, он требует прода.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

ok = fail = 0


def check(name, cond):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✓ {name}")
    else:
        fail += 1
        print(f"  ✗ {name}")


from bank_audit.loophole import repository as R          # noqa: E402
from bank_audit.loophole import db_schema as schema      # noqa: E402

print("\n— форма слияния —")
sql = R._fusion_cte(" WHERE is_loophole = TRUE", with_vec=True)
check("обе ноги на месте", "txt AS" in sql and "vec AS" in sql and "rel AS" in sql)
check("полнотекст русским словарём", "CAST('russian' AS regconfig)" in sql)
check("вес учитывается через ts_rank_cd", "ts_rank_cd(search_tsv" in sql)
check("слияние RRF, а не сумма оценок", f"1.0/({R._RRF_K} +" in sql)
check("фильтр пробрасывается в обе ноги", sql.count("is_loophole = TRUE") == 2)
check("при непустом WHERE добавляется AND", " AND\n               search_tsv @@" in sql
      or "AND search_tsv @@" in sql.replace("\n", " ").replace("  ", " "))

sql_nofilter = R._fusion_cte("", with_vec=True)
check("при пустом WHERE ставится WHERE", "WHERE search_tsv @@" in
      sql_nofilter.replace("\n", " ").replace("   ", " ").replace("  ", " "))

sql_novec = R._fusion_cte(" WHERE is_loophole = TRUE", with_vec=False)
check("без вектора нога пустая, а не отсутствует", "WHERE FALSE" in sql_novec)
check("без вектора запрос всё равно склеивается", "rel AS" in sql_novec)
check("без вектора плейсхолдера qvec нет", ":qvec" not in sql_novec)
check("различает суть записи и тело статьи",
      "CAST('{0,1,1,1}' AS float4[])" in sql)
check("суть выше по рангу внутри словесной ноги",
      "ORDER BY ts_rank_cd(CAST('{0,1,1,1}'" in " ".join(sql.split()))

print("\n— поля доезжают до разметки —")
# Замер на проде поймал ровно это: in_claim появился в txt, но не в rel, и
# запрос упал на ORDER BY. Проверяем всю цепочку, а не отдельные звенья.
_rel = sql.split("rel AS (")[1]
check("rel отдаёт in_claim наружу", "AS in_claim" in _rel)
_src = inspect_src = __import__("inspect").getsource(R)
for fn, name in ((R.search_relevant, "search_relevant"), (R.list_records, "list_records")):
    body = __import__("inspect").getsource(fn)
    check(f"{name} выбирает все поля разметки",
          all(f"rel.{c}" in body for c in ("via_txt", "via_vec", "in_claim")))
    check(f"{name} сортирует по сути записи",
          "rel.in_claim DESC" in body)

print("\n— происхождение совпадения —")
rows = [
    {"title": "Обход комиссии", "via_txt": True, "via_vec": True, "in_claim": True},
    {"title": "Дробление вкладов", "via_txt": True, "via_vec": False, "in_claim": True},
    {"title": "Квази-кэш", "via_txt": False, "via_vec": True, "in_claim": False},
]
out = R._mark_via([dict(r) for r in rows])
check("совпало и словами и смыслом", out[0]["via"] == "слова и смысл")
check("только словами", out[1]["via"] == "слова")
check("только смыслом", out[2]["via"] == "смысл")
check("служебные поля не утекают",
      all("via_txt" not in r and "via_vec" not in r and "in_claim" not in r
          for r in out))

print("\n— упоминание в статье это не попадание —")
ment = R._mark_via([
    {"title": "Дробление вкладов", "via_txt": True, "via_vec": True, "in_claim": False},
])
check("слово только в теле статьи помечено честно",
      ment[0]["via"] == "упоминание в статье")
check("вектор не превращает упоминание в совпадение",
      "слова" not in ment[0]["via"])

print("\n— дубли одного сюжета —")
dup = R._mark_via([
    {"title": "Ипотека от застройщика", "url": "https://a.ru/1", "via_txt": True},
    {"title": "Ипотека от застройщика", "url": "https://b.ru/2", "via_txt": True},
    {"title": "Другая лазейка", "url": "https://c.ru/3", "via_txt": True},
])
check("одинаковый заголовок схлопнут", len(dup) == 2)
check("выживает первый по релевантности", dup[0]["url"] == "https://a.ru/1")
check("регистр заголовка не создаёт дубль",
      len(R._mark_via([{"title": "Лазейка", "via_txt": True},
                       {"title": "ЛАЗЕЙКА", "via_txt": True}])) == 1)
check("пустой заголовок не схлопывает разные записи",
      len(R._mark_via([{"title": "", "url": "https://a.ru/1", "via_txt": True},
                       {"title": "", "url": "https://b.ru/2", "via_txt": True}])) == 2)

print("\n— вектор запроса —")
import bank_audit.rag.embedder as E                       # noqa: E402
_real = E.embed_one
try:
    E.embed_one = lambda t: (_ for _ in ()).throw(RuntimeError("модель недоступна"))
    check("недоступный эмбеддер не роняет поиск", R._query_vector("вклад") is None)
    E.embed_one = lambda t: [0.0] * 1024
    check("нулевой вектор отвергается", R._query_vector("вклад") is None)
    E.embed_one = lambda t: [0.5] + [0.0] * 1023
    v = R._query_vector("вклад")
    check("нормальный вектор оформлен для pgvector",
          v is not None and v.startswith("[0.500000,") and v.endswith("]"))
finally:
    E.embed_one = _real

print("\n— служебные колонки не попадают в API —")
import inspect                                            # noqa: E402
# Ищем именно SQL, а не упоминание в комментарии: в самом get_record стоит
# пояснение, почему звёздочки там больше нет.
_src = "\n".join(l for l in inspect.getsource(R.get_record).splitlines()
                 if not l.lstrip().startswith("#"))
check("get_record перечисляет поля явно, а не звёздочкой",
      "SELECT * FROM" not in _src and "_RECORD_FIELDS" in _src)
check("search_tsv не в списке полей записи", "search_tsv" not in R._RECORD_FIELDS)
check("embedding не в списке полей записи", "embedding" not in R._RECORD_FIELDS)
check("содержательные поля на месте",
      all(f in R._RECORD_FIELDS for f in
          ("title", "url", "snippet", "verdict_reason", "published_at", "raw_text")))

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
