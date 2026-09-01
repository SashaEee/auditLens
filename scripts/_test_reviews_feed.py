"""Этап 5: лента «Отзывов» — период, постраничность, принадлежность банку.

Проверяем контракт без обращения к базе: сигнатуры, порядок аргументов и то,
что параметры не теряются по дороге. Именно потеря параметра, а не ошибка
вычисления, дала два замечания: период не доходил до ленты вовсе.
"""
import ast
import inspect
import pathlib
import sys

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


from bank_audit.rag import reviews_dash as RD                 # noqa: E402

print("\n— период и смещение доходят до ленты —")
sig = inspect.signature(RD.list_reviews_ex).parameters
check("list_reviews_ex принимает days", "days" in sig)
check("list_reviews_ex принимает offset", "offset" in sig)
check("offset идёт ПОСЛЕДНИМ", list(sig)[-1] == "offset")

fsig = inspect.signature(RD._feed_from_index).parameters
check("_feed_from_index принимает offset", "offset" in fsig)

src = inspect.getsource(RD.list_reviews_ex)
check("поиск получает запас под смещение", "k=limit + offset + 1" in src)
# Поиск обрезает выдачу ровно до k. Без запроса лишней записи признак «есть
# ещё» тождественно ложен, и кнопка догрузки не появляется никогда.
check("запрошена лишняя запись ради признака «есть ещё»",
      "limit + offset + 1" in src and "len(res) > offset + limit" in src)
check("страница режется срезом, а не SQL-OFFSET", "res[offset:offset + limit]" in src)
check("на последней странице не проваливаемся в запасную ветку",
      'res["items"] or res["error"] or offset' in src)

feed_src = inspect.getsource(RD._feed_from_index)
check("запас выборки учитывает смещение", "(limit + offset) * 5" in feed_src)
check("лента отдаёт признак «есть ещё»", '"has_more"' in feed_src)

print("\n— эндпоинты не теряют параметры —")
app_src = (pathlib.Path(__file__).resolve().parents[1]
           / "src/bank_audit/web/app.py").read_text(encoding="utf-8")
tree = ast.parse(app_src)
eps = {}
for n in ast.walk(tree):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
            n.name in ("reviews_feed", "reviews_feed_classified"):
        eps[n.name] = n
for name in ("reviews_feed", "reviews_feed_classified"):
    node = eps.get(name)
    check(f"{name} найден", node is not None)
    if not node:
        continue
    args = [a.arg for a in node.args.args]
    check(f"{name} принимает days", "days" in args)
    check(f"{name} принимает offset", "offset" in args)
    body = ast.get_source_segment(app_src, node) or ""
    check(f"{name} передаёт days дальше", "days=days" in body)
    check(f"{name} передаёт offset дальше", "offset=max(0, offset)" in body)

cls = eps.get("reviews_feed_classified")
if cls:
    body = ast.get_source_segment(app_src, cls) or ""
    # Позиционный вызов молча выбрасывал период пятым аргументом.
    check("classified зовёт list_reviews_ex именованными аргументами",
          "product=product" in body and "days=days" in body)
    check("functools импортирован там, где используется",
          "import functools" in body and "functools.partial" in body)

print("\n— банк виден аудитору —")
jsx = (pathlib.Path(__file__).resolve().parents[1]
       / "src/bank_audit/web/static/app.jsx").read_text(encoding="utf-8")
check("банк подписан на карточке обращения", "rv-pill-bank" in jsx)
check("банк есть в подзаголовке полного текста",
      "sub={[modalRev.bank," in jsx)
check("лента запрашивает период", "&days=${days}&limit=20&offset=" in jsx)
check("есть кнопка догрузки", "loadMoreFeed" in jsx and "feedMore&&" in jsx)
check("догрузка не занимает состояние вкладки «Рынок»",
      "setFeedMoreBusy" in jsx)

print("\n— лента отдаёт банк наружу —")
check("_feed_from_index кладёт bank в элемент", '"bank": r["bank"]' in feed_src)

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
