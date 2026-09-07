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
all_src = "\n".join(
    pathlib.Path(f).read_text(encoding="utf-8")
    for f in pathlib.Path("src/bank_audit").rglob("*.py"))
import re as _re
_calls = _re.findall(r"list_reviews_ex[,(]\s*([^)]{0,200})", all_src)
_positional = [c for c in _calls
               if len([a for a in c.split(",")[1:] if a.strip() and "=" not in a]) > 0]
check("никто не зовёт list_reviews_ex позиционно дальше первого аргумента",
      not _positional)

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
check("лента запрашивает период", "&days=${days}" in jsx and "offset=${off}" in jsx)
check("есть кнопка догрузки", "loadMoreFeed" in jsx and "feedMore&&" in jsx)
check("догрузка не занимает состояние вкладки «Рынок»",
      "setFeedMoreBusy" in jsx)

print("\n— регуляторная эскалация как фильтр —")
check("list_reviews_ex принимает esc", "esc" in sig)
check("_feed_from_index принимает esc", "esc" in fsig)
check("в ленте это один булев предикат по индексу", 'extra += " AND i.esc"' in feed_src)
# Поиск идёт по чужому корпусу, где признака нет. Без отбора после поиска
# фильтр молча пропадал бы, стоило ввести запрос — ровно как раньше период.
check("фильтр не пропадает в режиме поиска", "keep = _esc_urls(" in src)
check("множество url считается ОДИН раз, а не на элемент",
      src.count("_esc_urls(") == 1)
esc_src = inspect.getsource(RD._esc_urls)
check("сбой признака не сужает выдачу молча", "return set(urls)" in esc_src)
check("эндпоинт принимает esc", "esc: int = 0" in app_src)

print("\n— плашки ведут к обращениям —")
check("эскалация кликабельна", "setEscOnly" in jsx and "rv-kpi-click" in jsx)
check("лента запрашивает эскалацию", 'escOnly?"&esc=1"' in jsx)
import re as _re2
_deps = _re2.search(r"\},\[(bank,product,theme[^\]]*)\]\);", jsx)
_deps = _deps.group(1) if _deps else ""
check("каждый фильтр перезапрашивает ленту",
      all(d in _deps for d in ("bank", "product", "theme", "q", "days", "escOnly")))
check("главная тема кликабельна", "setTheme(t=>t===th.themes[0].key" in jsx)

print("\n— порядок выдачи поиска —")
check("list_reviews_ex принимает sort", "sort" in sig)
check("по дате сортируем ДО среза страницы",
      src.index('sort == "date"') < src.index("res[offset:offset + limit]"))
check("сортировка не трогает ленту без запроса",
      'if sort == "date":' in src and src.count('sort == "date"') == 1)
check("эндпоинт принимает sort", 'sort: str = "auto"' in app_src)
check("эндпоинт передаёт sort дальше", "sort=sort" in app_src)
check("переключатель есть только при запросе", 'q&&<div className="rv-sort"' in jsx)
check("выбор порядка перезапрашивает ленту", "sortBy" in _deps)
check("подсказка честна: отбирает релевантность",
      "Отбирает всё равно релевантность" in jsx)

print("\n— ненадёжная метка продукта названа —")
check("происхождение метки продукта названо", "rv-warn-inline" in jsx
      and "определён по тексту" in jsx)

print("\n— лента отдаёт банк наружу —")
check("_feed_from_index кладёт bank в элемент", '"bank": r["bank"]' in feed_src)

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
