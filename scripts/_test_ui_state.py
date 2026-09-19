"""Состояние разделов и пояснение методик — по замечаниям аудиторов.

10 и 162: при переключении между разделами терялась вся загруженная
информация. 151 и 153: непонятно, как считаются баллы банков и рейтинг.
121: «База знаний» логичнее в группе «Данные».
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
JSX = (ROOT / "src/bank_audit/web/static/app.jsx").read_text(encoding="utf-8")
HTML = (ROOT / "src/bank_audit/web/static/index.html").read_text(encoding="utf-8")

ok = fail = 0


def check(name, cond):
    global ok, fail
    ok, fail = ok + bool(cond), fail + (not cond)
    print(("  ✓ " if cond else "  ✗ ") + name)


print("— разделы не теряют загруженное —")
check("посещённые разделы остаются смонтированными", "keptPages.map(id=>{" in JSX)
check("неактивный раздел скрывается, а не удаляется", 'display:on?"block":"none"' in JSX)
check("число удерживаемых ограничено", "KEEP_PAGES=4" in JSX and "next.slice(0,KEEP_PAGES)" in JSX)
check("последний открытый — первым в списке", "[page,...prev.filter(x=>x!==page)]" in JSX)
check("тяжёлые разделы по-прежнему отдельно", "loopholeMounted&&" in JSX and "aiMounted&&" in JSX)
check("скрытый раздел помечен для доступности", "aria-hidden={!on}" in JSX)
check("параметры адреса получает только активный", "params={on?pageParams:undefined}" in JSX)

print("\n— прокрутка запоминается —")
check("позиция сохраняется при уходе", "scrollPos.current[prevPage.current]=el.scrollTop" in JSX)
check("позиция возвращается при входе", "contentRef.current.scrollTop=y||0" in JSX)
check("контейнер прокрутки связан со ссылкой", '<div className="content" ref={contentRef}>' in JSX)

print("\n— методика на виду —")
check("компонент пояснения есть", "function MethodNote({title, children})" in JSX)
check("свёрнуто по умолчанию", re.search(r"function MethodNote[\s\S]{0,200}useState\(false\)", JSX) is not None)
check("во вкладке «Банки» объяснены балл и место", "Балл и место</b> считает banki.ru" in JSX)
check("объяснено, от чего считается доля решённых", "от проверенных площадкой" in JSX)
check("предупреждение о чтении доли в отрыве от числа отзывов", "читать долю в отрыве от неё нельзя" in JSX)
check("объяснено расхождение с площадкой", "мы показываем состояние" in JSX and "на дату сбора" in JSX)
check("стили пояснения добавлены", ".method-note-body{" in HTML and ".method-note-btn{" in HTML)

print("\n— навигация —")
nav = JSX[JSX.index("const NAV="):JSX.index("const NAV=") + 1200] if "const NAV=" in JSX else JSX
check("«База знаний» в группе «Данные»",
      re.search(r'id:"knowledge".{0,80}group:"Данные"', JSX) is not None)
check("«База знаний» больше не в «Анализе»",
      re.search(r'id:"knowledge".{0,80}group:"Анализ"', JSX) is None)
check("порядок разделов данных сохранён",
      JSX.index('id:"knowledge"') < JSX.index('id:"banks"') < JSX.index('id:"sources"'))

print("\n— кэш интерфейса обновлён —")
check("кэш-бастер новый", "app.jsx?v=20260911-method" in HTML)

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
