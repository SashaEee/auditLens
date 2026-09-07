"""Обобщение таксономии отзывов на два измерения: тема и продукт.

Главное, что проверяем, — что тема НЕ изменилась: она работает на проде,
и обобщение не должно её задеть.
"""
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


from bank_audit.rag import review_topics as RT      # noqa: E402

print("\n— тема осталась поведением по умолчанию —")
for fn in ("active_version", "discover", "finalize", "store", "assign",
           "label_new", "topics", "is_ready", "status", "rebuild", "seed_names",
           "_normalize"):
    sig = inspect.signature(getattr(RT, fn)).parameters
    check(f"{fn}: dim по умолчанию — тема",
          "dim" in sig and sig["dim"].default == RT.THEME)

check("ключ состояния темы БЕЗ префикса", RT._skey(RT.THEME, "active_version") == "active_version")
check("ключ состояния продукта с префиксом",
      RT._skey(RT.PRODUCT, "active_version") == "product:active_version")

print("\n— у продукта своя настройка —")
t, pr = RT._dim(RT.THEME), RT._dim(RT.PRODUCT)
check("у темы есть риск", t.has_risk)
check("у продукта риска нет", not pr.has_risk)
check("промпты разные", t.see != pr.see and t.merge != pr.merge)
check("промпт продукта просит продукт, а не проблему",
      "а НЕ проблему" in pr.see)
check("рамки продукта отдельные", pr.target != t.target or pr.minimum != t.minimum)

print("\n— разбор ответа модели —")
theme_line = "blocking | Блокировки счетов | compliance | клиенты пишут что счёт заблокировали без объяснения"
prod_line = "deposit | Вклад | клиенты пишут про вклад срочный вклад проценты по вкладу и снятие"
a = RT._parse_taxonomy([theme_line], RT.THEME)
check("тема разобрана с риском", a and a[0]["risk"] == "compliance")
b = RT._parse_taxonomy([prod_line], RT.PRODUCT)
check("продукт разобран без риска", b and b[0]["risk"] is None)
check("продукт взял описание из третьего поля",
      b and b[0]["descr"].startswith("клиенты пишут про вклад"))
check("строка темы НЕ проходит как продукт-с-риском",
      RT._parse_taxonomy([theme_line], RT.PRODUCT)[0]["risk"] is None)
check("трёхполевая строка не годится в тему",
      RT._parse_taxonomy([prod_line], RT.THEME) == [])

print("\n— измерения не смешиваются в SQL —")
src = pathlib.Path("src/bank_audit/rag/review_topics.py").read_text(encoding="utf-8")
asg = inspect.getsource(RT.assign)
check("assign чистит метки ТОЛЬКО своего измерения",
      "DELETE FROM review_topic_label WHERE topic_id IN" in asg and "d.dim" not in asg.split("DELETE")[1][:200]
      or "WHERE dim = :dim" in asg)
norm = inspect.getsource(RT._normalize)
check("статистика считается внутри измерения", "WHERE d.dim = :dim" in norm)
check("ранг считается внутри измерения", norm.count("d.dim = :dim") >= 2)
check("topics() читает своё измерение",
      "dim = :dim AND version = :v" in inspect.getsource(RT.topics))
check("is_ready() смотрит своё измерение", "d.dim = :dim" in inspect.getsource(RT.is_ready))
check("label_new ищет неразмеченных в своём измерении",
      "d.dim = :dim" in inspect.getsource(RT.label_new))

print("\n— словарь продуктов берётся из корпуса —")
seed = inspect.getsource(RT.seed_names)
check("на первом прогоне продукт берёт словарь площадки",
      "SELECT DISTINCT product FROM review_index" in seed)
check("тема свой словарь не потеряла", "prev = topics(dim=dim)" in seed)

print("\n— перенос разметки в индекс —")
ap = inspect.getsource(RT.apply_product_labels)
check("берём строго лучший продукт", "l.rn = 1" in ap)
check("порог по нормированной оценке применяется", "l.z >= :minz" in ap)
check("не отнесённые ОЧИЩАЮТСЯ, а не хранят старую метку", "SET product = NULL" in ap)
check("работает только со своим измерением", ap.count(":dim") >= 2)

sched = pathlib.Path("src/bank_audit/digest/scheduler.py").read_text(encoding="utf-8")
check("планировщик размечает и продукт", "dim=review_topics.PRODUCT" in sched)
check("планировщик переносит метки в индекс", "apply_product_labels" in sched)
check("продукт размечается только когда таксономия есть",
      "if review_topics.active_version(review_topics.PRODUCT):" in sched)

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
