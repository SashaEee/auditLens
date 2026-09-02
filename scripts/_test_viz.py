"""Визуализация: шаблон без цифр, плейсхолдеры, санитайзер, якоря, бейджи.

Проверки повторяют находки панели критики 03.09: цифры и скобки в шаблоне,
литеральные цвета, url() в svg, class к CSS приложения, отрицательные
отступы, скрытие содержимого, логотип по чужому пути, маркер от модели.
"""
import asyncio
import pathlib
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from bank_audit.research.gptr import viz as V   # noqa: E402

ok = fail = 0


def check(name, cond):
    global ok, fail
    ok, fail = ok + bool(cond), fail + (not cond)
    print(("  ✓ " if cond else "  ✗ ") + name)


def rejected(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return None
    except V.VizRejected as e:
        return str(e)


def fact(i, subject, attribute, value, unit="", date="", verbatim="", stance="declared"):
    return NS(id=i, subject=subject, attribute=attribute, value=value, unit=unit,
              date=date, verbatim=verbatim, url="u" + str(i), stance=stance)


F = [fact(1, "sberbank", "Ставка", "от 1,25", "%", "2026-08-01", "комиссия от 1,25% от оборота"),
     fact(2, "tbank", "Ставка", "1,7", "%", "2026-07-15", "1,7% при обороте от 500 000 ₽"),
     fact(3, "sberbank", "Срок подключения", "2", "дня", "2026-08-01", "установим за 2 дня <script>"),
     fact(4, "vtb", "Ставка", "1,8–2,5", "%", "2026-07-01", "", stance="observed")]
L = {"sberbank": "Сбербанк", "tbank": "Т-Банк", "vtb": "ВТБ"}
SUBJ = ["sberbank", "tbank", "vtb"]
KW = dict(facts=F, labels=L, section="market", subjects=SUBJ)


def prep(t, **kw):
    return V.prepare(t, **{**KW, **kw})


print("— шаблон: что запрещено модели —")
good = ('<div class="viz"><h4>Ставка</h4><span>{{name:sberbank}} {{f:1}} {{f:1.cite}} {{f:1.date}}</span>'
        '<span>{{name:tbank}} {{f:2}} {{f:2.cite}} {{f:2.date}}</span>'
        '<small>Показано {{meta:facts_used}} из {{meta:facts_total}}; объектов {{meta:subjects}}</small></div>')
p = prep(good)
check("корректный шаблон проходит", "от 1,25 %" in p.html and "1,7 %" in p.html and p.fact_ids == [1, 2])
check("цифра руками отклоняется, с контекстом", "Ставка 2026" in (rejected(prep, good.replace("<h4>Ставка</h4>", "<h4>Ставка 2026</h4>")) or ""))
check("надстрочная цифра отклоняется", rejected(prep, good.replace("<h4>Ставка</h4>", "<h4>м²</h4>")) is not None)
check("квадратные скобки отклоняются", "скобки" in (rejected(prep, good.replace("<h4>Ставка</h4>", "<h4>[x]</h4>")) or ""))
check("литеральный цвет отклоняется", "цвет" in (rejected(prep, good.replace('class="viz"', 'class="viz" style="color:#fff"')) or ""))
check("rgb() отклоняется", rejected(prep, good.replace('class="viz"', 'class="viz" style="color:rgb(0,0,0)"')) is not None)
check("число без якоря рядом отклоняется", "якоря" in (rejected(prep, good.replace("{{f:1.cite}} ", "")) or ""))
check("якорь далеко (> окна) не считается", rejected(prep, good.replace("{{f:1.cite}} ", "x" * 300 + " {{f:1.cite}} ")) is not None)
check("сравнение без даты отклоняется", "даты" in (rejected(prep, good.replace("{{f:1.date}}", "").replace("{{f:2.date}}", "")) or ""))
check("один объект — дата не обязательна", rejected(prep, '<div class="viz">{{f:1}} {{f:1.cite}}</div>') is None)
check("факт не из раздела отклоняется", "не из этого раздела" in (rejected(prep, good.replace("{{f:2}}", "{{f:99}}")) or ""))
check("неизвестный плейсхолдер отклоняется", "неизвестный" in (rejected(prep, good.replace("{{name:sberbank}}", "{{color:sberbank}}")) or ""))
check("незакрытые скобки плейсхолдера отклоняются", rejected(prep, good.replace("{{name:sberbank}}", "{{name:sberbank}")) is not None)
check("логотип вне сравнения отклоняется", "только в сравнении" in (rejected(prep, good.replace("{{name:sberbank}}", "{{logo:sberbank}}"), section="voice") or ""))
check("логотип чужого объекта отклоняется", rejected(prep, good.replace("{{name:sberbank}}", "{{logo:../../etc/passwd}}")) is not None and "неизвестного" in (rejected(prep, good.replace("{{name:sberbank}}", "{{logo:alfabank}}")) or ""))
check("логотип в сравнении — сентинель", V._S_LOGO in prep(good.replace("{{name:sberbank}}", "{{logo:sberbank}}")).html)
check("якорь внутри тега отклоняется", "внутри тега" in (rejected(prep, good.replace('<h4>Ставка</h4>', '<h4 style="{{f:1.cite}}">С</h4>')) or ""))
check("служебный символ в шаблоне отклоняется", rejected(prep, good + "") is not None)
check("слишком большой шаблон отклоняется", rejected(prep, good + "x" * 70_000) is not None)
q = prep('<div class="viz"><p>{{f:3.quote}} {{f:3.cite}} {{f:3.side}} {{f:4.side}} {{f:4.cite}} {{f:4.date}} {{f:3.date}}</p></div>')
check("цитата экранирована, скобки в значениях тоже", "&lt;script&gt;" in q.html and "<script>" not in q.html)
check("метка стороны подставлена", "заявлено" in q.html and "наблюдается" in q.html)
check("meta-счётчики подставлены", "Показано 2 из 4; объектов 3" in V.visible_text(p.html))

print("\n— выходные числа: страховка —")
meta = V.meta_for(F); meta["facts_used"] = "2"
check("числа фактов проходят", rejected(V.check_output_numbers, "<b>от 1,25 % 1.7 500 000</b>", F[:2], meta) is None)
check("чужое число ловится", "2.14" in (rejected(V.check_output_numbers, "<b>2,14%</b>", F[:2], meta) or ""))
check("meta-числа проходят", rejected(V.check_output_numbers, "<b>4 3 2</b>", F[:2], meta) is None)

print("\n— санитайзер —")
c = V.sanitize('<div class="viz modal-backdrop" style="color:var(--ink);background-color:url(x);position:fixed;display:flex;margin-top:-1500px;opacity:0">'
               '<svg viewBox="0 0 10 10"><rect width="4" height="4" fill="url(http://evil/x.svg#g)" onload="x()"/>'
               '<path d="M0 0 L1 1" fill="var(--pos)"/><foreignObject><b>y</b></foreignObject><text x="1" fill="#000">t</text>'
               '<defs><linearGradient id="g"/></defs><title>888</title></svg><script>alert(1)</script><a href="javascript:x">l</a><img src="x"><!-- c --></div>')
check("class оставлен только viz", 'class="viz"' in c and "modal" not in c)
check("опасные стили выброшены, безопасные остались", "url(" not in c.split("<svg")[0] and "fixed" not in c and "-1500" not in c and "opacity" not in c and "color:var(--ink)" in c and "display:flex" in c)
check("внешний url в fill убран, палитра осталась", 'fill="url(' not in c and 'fill="var(--pos)"' in c)
check("литеральный цвет в fill убран", 'fill="#000"' not in c)
check("скрипт, ссылка, картинка, foreignObject, defs, title убраны", all(t not in c for t in ("<script", "<a ", "<img", "foreignObject", "alert", "<defs", "<title", "888")))
check("обработчик события убран", "onload" not in c)
check("комментарий убран", "<!--" not in c)
check("корневой svg получает ширину 100%", 'width="100%"' in c and 'viewBox="0 0 10 10"' in c)
check("svg без viewBox отклоняется", "viewBox" in (rejected(V.sanitize, '<div class="viz"><svg><rect/></svg></div>') or ""))
check("style-тег вырезан с содержимым", "display:none" not in V.sanitize("<div><style>*{display:none}</style>ok</div>"))
check("clean_style: экранирование и функции", V.clean_style("width:expression(1);color:red;background-color:\\75rl(x)") is None
      and V.clean_style("color:red;width:expression(1)") == "color:red")
check("clean_style: var только из палитры", V.clean_style("color:var(--ink);background-color:var(--evil)") == "color:var(--ink)")
check("clean_style: display только раскладка", V.clean_style("display:none;display:grid") == "display:grid")
check("clean_style: размер шрифта в границах", V.clean_style("font-size:0;font-size:14px") == "font-size:14px")
check("clean_style: ширина в границах", V.clean_style("width:5000px;height:2000px;width:100%") == "width:100%")
check("clean_style: repeat() отклонён, 1fr 1fr прошёл", V.clean_style("grid-template-columns:repeat(3,1fr);grid-template-columns:1fr 1fr") == "grid-template-columns:1fr 1fr")
check("transform в границах", V._transform_ok("translate(10,20) scale(2)") and not V._transform_ok("translate(-3000,0)") and not V._transform_ok("matrix(1,0,0,1,0,0)"))
check("слишком много путей отклоняется", "путей" in (rejected(V.sanitize, '<div class="viz"><svg viewBox="0 0 1 1">' + '<path d="M0 0"/>' * 41 + '</svg></div>') or ""))

print("\n— бейджи и цвета —")
check("фирменный цвет известного банка", V.brand_color("sberbank") == "#21A038" and V.brand_color("tbank") == "#FFDD2D")
check("неизвестный слаг — детерминированный цвет", V.brand_color("unknown_6e2b") == V.brand_color("unknown_6e2b"))
check("на жёлтом — тёмный текст", V._text_on("#FFDD2D") != "#FFFFFF" and V._text_on("#0A2896") == "#FFFFFF")
check("инициалы", V.initials("Т-Банк") == "Т" and V.initials("Хоум Кредит") == "ХК" and V.initials("ВБРР") == "ВБ" and V.initials("Банк «Открытие»") == "О")
badge = V.logo_svg("sberbank", "Сбербанк")
check("монограмма — svg с фирменным цветом", badge.startswith("<svg") and "#21A038" in badge and ">С<" in badge)
check("монограмма проходит финальную очистку без потерь", "#21A038" in V._nh3(badge, final=True) and "<text" in V._nh3(badge, final=True))
V.LOGO_DIR = "/nonexistent"; V._official_logo.cache_clear()
check("нет каталога логотипов — монограмма", V.logo_svg("vtb", "ВТБ").startswith("<svg"))
check("обход каталога невозможен", V._official_logo("../../etc/passwd") == "" and V._official_logo("a/b") == "")

print("\n— ответ модели → блок → финал —")
ans = ("Вот:\n```html\n" + good.replace("{{name:tbank}}", "{{logo:tbank}}") + "\n```\n```html\n<div class=\"viz\">второй лишний</div>\n```")
b = V.build(ans, **KW)
check("принят один блок, второй за лимитом раздела", b.html.count('class="viz"') == 1 and not b.rejected)
check("факты и логотипы учтены", b.fact_ids == [1, 2] and "tbank" in b.logos)
check("якоря ещё сентинели", V._S_CITE in b.html and "[f:" not in b.html)
bad = V.build("```html\n<div class=\"viz\">2,14% руками</div>\n```", **KW)
check("блок с цифрой отклонён с причиной", bad.html == "" and bad.rejected and "цифра" in bad.rejected[0])
check("ПУСТО → нет блоков", V.parse_blocks("ПУСТО") == [] and V.parse_blocks("") == [])
check("блок не с <div — игнорируется", V.parse_blocks("```html\n<svg></svg>\n```") == [])
check("два ограждения для карты условий", len(V.parse_blocks("```html\n<div>a</div>\n```\n```html\n<div>b</div>\n```", 2)) == 2)
fin = V.finalize(b.html, b.logos, cite=lambda fid: {1: 3, 2: 5}[fid])
check("finalize: якоря стали sup с data-cite", 'data-cite="3">3</sup>' in fin and 'data-cite="5">5</sup>' in fin)
check("finalize: логотип вставлен и пережил финальную очистку", "<svg" in fin and "#FFDD2D" in fin)
check("finalize: служебных символов нет", not V._SENTINELS.search(fin))
check("finalize: неизвестный источник — отказ", rejected(V.finalize, b.html, b.logos, cite=lambda fid: None) is not None)
check("resanitize чужой разметки", V.resanitize('<div class="viz"><script>x</script><b onclick="y">ok</b></div>') == '<div class="viz"><b>ok</b></div>')

print("\n— маркер —")
check("маркер имеет ожидаемую форму", V.marker(3) == "\n\n[[VIZ:3]]\n\n")


async def _gen(*xs):
    for x in xs:
        yield x


async def _collect(*xs):
    return "".join([p async for p in V.without_markers(_gen(*xs))])


run = lambda *xs: asyncio.run(_collect(*xs))
check("маркер от модели обезвреживается", "[[VIZ:0]]" not in run("текст [[VIZ:0]] далее") and "далее" in run("текст [[VIZ:0]] далее"))
check("маркер, разорванный между кусками, тоже", "[[VIZ:1]]" not in run("а [[VI", "Z:1]] б"))
check("обычный текст с [[ не ломается", run("см. [[скобки]] и [", " ещё") == "см. [[скобки]] и [ ещё")
check("strip_markers убирает маркеры", V.strip_markers("а\n\n[[VIZ:2]]\n\nб") == "а\n\nб")

print("\n— промпт —")
pr = V.designer_prompt(section="market", title="Сбербанк против рынка", question="Q", anchor="sberbank",
                       labels=L, facts_text="[f:1] …", section_text="текст", subjects=SUBJ)
check("промпт: нет цифр, якорь у числа, дата, палитра, покрытие, ПУСТО", all(k in pr for k in
      ("нет ни одной цифры", "{{f:12.cite}}", "{{f:12.date}}", "var(--ink)", "{{meta:facts_used}}", "ПУСТО", "Точка отсчёта: Сбербанк")))
check("промпт: запреты дословно", "кодировать величину длиной" in pr and "эмодзи" in pr)
check("промпт: color-плейсхолдера нет", "{{color:" not in pr)
check("промпт: форма раздела", "Лестница ранжирования" in pr)

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
