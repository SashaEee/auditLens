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
check("сравнение без даты отклоняется", "дат" in (rejected(prep, good.replace("{{f:1.date}}", "").replace("{{f:2.date}}", "")) or ""))
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
check("clean_style: экранирование и функции", V.clean_style("width:expression(1);color:var(--ink);background-color:\\75rl(x)") is None
      and V.clean_style("color:var(--ink);width:expression(1)") == "color:var(--ink)")
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
V.LOGO_DIR = "/nonexistent"; V._LOGO_CACHE.clear()
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
check("промпт: пример ячейки и запрет «500 тыс.»", "<td>{{f:12}} {{f:12.cite}}" in pr and "«500 тыс.»" in pr)
rp = V.repair_prompt(pr, "<div>старый</div>", ["блок 1: цифра"])
check("repair_prompt: причины и прежний ответ", "ОТКЛОНЁН" in rp and "блок 1: цифра" in rp and "старый" in rp and rp.startswith(pr[:200]))


print("\n— по находкам состязательной проверки —")
check("{{f:N.value}} без единицы, {{f:N}} с единицей", "от 1,25 %" in prep(good).html and prep('<div class="viz">{{f:1.value}} {{f:1.cite}}</div>').html.count("%") == 0)
check("цифры категорий No/Nl отклоняются", rejected(prep, good.replace("<h4>Ставка</h4>", "<h4>⑤ Ⅻ</h4>")) is not None)
check("сущность-сентинель отклоняется", "служебные" in (rejected(prep, good.replace("<h4>Ставка</h4>", "<h4>&#xE001;7&#xE001;</h4>")) or ""))
check("CDATA и комментарии отклоняются", "CDATA" in (rejected(prep, good.replace("<h4>Ставка</h4>", "<h4><![CDATA[2026]]></h4>")) or ""))
check("ложный тег <2026> ловится", rejected(prep, good.replace("<h4>Ставка</h4>", "<h4>x <2026> y</h4>")) is not None)
check("плейсхолдер значения внутри атрибута отклоняется", rejected(prep, good.replace('<h4>Ставка</h4>', '<h4 style="width:{{f:1.value}}%">С</h4>')) is not None and "внутри тега" in (rejected(prep, good.replace('<h4>Ставка</h4>', '<h4 style="width:{{f:1.value}}%">{{f:1.cite}}</h4>')) or ""))
check("якорь внутри svg отклоняется", "внутри svg" in (rejected(prep, '<div class="viz"><svg viewBox="0 0 1 1"><text>{{f:1}} {{f:1.cite}}</text></svg></div>') or ""))
Fpua = [fact(9, "sberbank", "Ставка", "1,9\ue001 7\ue001", "%", "2026-08-01", "x")]
check("сентинель в значении факта вычищается", "\ue001" not in V.prepare('<div class="viz">{{f:9}} {{f:9.cite}}</div>', facts=Fpua, labels=L, section="market", subjects=SUBJ).html.replace(V._S_CITE + "9" + V._S_CITE, ""))
check("clean_style: -.5em и 1e9px отклоняются", V.clean_style("margin-top:-.5em;width:1e9px;color:var(--ink)") == "color:var(--ink)")
check("clean_style: именованный цвет и transparent для текста отклоняются", V.clean_style("color:white;color:transparent;color:var(--surface);color:var(--ink)") == "color:var(--ink)")
check("clean_style: фон только фоновыми токенами", V.clean_style("background-color:var(--ink);background-color:var(--surface)") == "background-color:var(--surface)")
check("clean_style: font-size в em отклоняется, line-height:0 отклоняется", V.clean_style("font-size:0.85em;line-height:0;font-size:14px;line-height:1.4") == "font-size:14px;line-height:1.4")
check("clean_style: граница с палитрой проходит", V.clean_style("border:1px solid var(--hair);border-left:3px solid var(--accent)") == "border:1px solid var(--hair);border-left:3px solid var(--accent)")
check("clean_style: margin 2000px отклоняется", V.clean_style("margin-left:2000px;padding:8px") == "padding:8px")
check("viewBox с диким соотношением — блок отклонён", "viewBox" in (rejected(V.sanitize, '<div class="viz"><svg viewBox="0 0 1 100"><rect width="1" height="1"/></svg></div>') or ""))
c2 = V.sanitize('<div class="viz"><svg viewBox="0 0 10 10"><text fill="var(--surface)" opacity="0.15">t</text><rect fill-opacity="0.1" width="1" height="1"/></svg><a href="x">якорь и дата</a><svg viewBox="0 0 10 10" transform="matrix(1,0,0,1,0,0)"><rect width="1" height="1" transform="scale(1000)"/></svg></div>')
check("текст цветом фона и opacity убраны", 'fill="var(--surface)"' not in c2 and "opacity" not in c2)
check("<a> снят, содержимое осталось", "якорь и дата" in c2 and "<a" not in c2)
check("transform вне границ убран", "scale(1000)" not in c2 and "matrix" not in c2)
check("исключение в фильтре не пропускает атрибут", 'transform="translate(1,"' not in V.sanitize('<div class="viz"><svg viewBox="0 0 1 1"><rect transform="translate(1," width="1" height="1"/></svg></div>'))
check("монограмма без цифр: «Банк 131» → Б", V.initials("Банк 131") == "Б" and V.initials("Точка 24 Банк") == "Т" and not any(ch.isdigit() for ch in V.initials("131")))
check("цифра из названия объекта не ложный отказ", rejected(V.check_output_numbers, "<b>Банк 131</b>", [], {}, {"x": "Банк 131"}) is None)
check("страховка: 20 не проходит за счёт «2»", "20" in (rejected(V.check_output_numbers, "<b>20</b>", F[2:3], {}) or ""))
check("resanitize: сентинели и лимиты", V.resanitize("a\ue001b") == "ab" and V.resanitize("<div>" + "x" * 90_000 + "</div>") == "")
class _C:
    def __init__(self): self.calls = []
    def __call__(self, fid): self.calls.append(fid); return 3
    def known(self, fid): return fid != 99
_c = _C()
check("finalize: неизвестный факт — отказ до регистрации источников", rejected(V.finalize, V._S_CITE + "99" + V._S_CITE, {}, _c) is not None and _c.calls == [])
_c2 = _C(); V.finalize("<div>" + V._S_CITE + "1" + V._S_CITE + "</div>", {}, _c2)
check("finalize: источники регистрируются один раз после проверки", _c2.calls == [1])


print("\n— синтез состязательной проверки: остаток —")
bld = lambda tpl, **kw: V.build("```html\n" + tpl + "\n```", **{**KW, **kw}).rejected
check("visible_text не разрывает число инлайн-тегом", V.visible_text("2<b>5</b>") == "25")
check("два значения вплотную отклоняются", any("вплотную" in r for r in bld('<div class="viz"><p>{{f:3.value}}<b>{{f:2.value}}</b> % {{f:3.cite}} {{f:2.cite}} {{f:3.date}} {{f:2.date}}</p></div>')))
check("_tokens: значение и дата рядом не склеиваются", V._tokens("2 2026-08-01") == {"2", "2026", "08", "01"} and V._tokens("500 000") == {"500000"})
check("якорь из соседней ячейки не считается", "якоря" in (rejected(prep, '<div class="viz"><table><tr><td>{{f:1}} {{f:2.cite}}</td><td>{{f:2}} {{f:1.cite}}</td></tr></table>{{f:1.date}} {{f:2.date}}</div>') or ""))
check("якорь в той же ячейке проходит", rejected(prep, '<div class="viz"><table><tr><td>{{f:1}} {{f:1.cite}} {{f:1.date}}</td><td>{{f:2}} {{f:2.cite}} {{f:2.date}}</td></tr></table></div>') is None)
check("grid-column выброшен", V.clean_style("grid-column:2") is None)
check("якорь в вырезаемом теге отклоняется", any("вырезается" in r for r in bld('<div class="viz"><p>{{f:1}} <title>{{f:1.cite}}</title> {{f:1.date}}</p></div>')))
check("дата третьего объекта не годится для сравнения", "дат" in (rejected(prep, '<div class="viz">{{f:1}} {{f:1.cite}} {{f:2}} {{f:2.cite}} на {{f:4.date}}</div>') or ""))
check("доска шагов без дат допустима", rejected(prep, '<div class="viz"><ol><li>{{f:1}} {{f:1.cite}}</li><li>{{f:2}} {{f:2.cite}}</li></ol></div>', section="checks") is None)
check("общая дата одного окна проходит", rejected(prep, '<div class="viz">{{f:1}} {{f:1.cite}} {{f:3}} {{f:3.cite}} на {{f:1.date}}</div>') is None)
check("meta вне строки покрытия отклоняется", any("small" in r for r in bld('<div class="viz"><p>жалоб {{meta:facts_total}} тыс. {{f:2}} {{f:2.cite}}</p></div>')))
check("два корня .viz отклоняются", any("один корень" in r for r in bld('<div class="viz"><p>{{f:1}} {{f:1.cite}}</p></div><div class="viz"><p>x</p></div>', section="voice")))
check("«online =» в тексте не ложный отказ", not bld(good.replace("<h4>Ставка</h4>", "<h4>тариф online = базовый</h4>")))
check("«#abc» в тексте не ложный отказ", rejected(prep, good.replace("<h4>Ставка</h4>", "<h4>тег #abc</h4>")) is None)
check("fill=none на тексте выброшен", 'fill="none"' not in V.sanitize('<div class="viz"><svg viewBox="0 0 1 1"><text fill="none">a</text></svg></div>'))
check("цепочка scale и rotate(180) отклоняются", not V._transform_ok("scale(0.1) scale(0.1)") and not V._transform_ok("rotate(180 50 10)") and V._transform_ok("translate(10,20) rotate(90)"))
check("list-style: только обычные маркеры", V.clean_style("list-style:upper-roman") is None and V.clean_style("list-style:disc") == "list-style:disc")
check("пустой <li> отклоняется", rejected(prep, '<div class="viz"><ol><li></li></ol>{{f:1}} {{f:1.cite}}</div>') is not None)
check("fill-rule сохраняется", 'fill-rule="evenodd"' in V._nh3('<svg viewBox="0 0 1 1"><path d="M0 0" fill-rule="evenodd"/></svg>', final=True))
g = V.MarkerGuard()
check("MarkerGuard: маркер из обрывков обезврежен", "[[VIZ:0]]" not in g.feed("[[") + g.feed("VIZ:0]] x") + g.finish())
check("MarkerGuard: обычный текст цел", (lambda g2: g2.feed("см. [[скобки]] и [") + g2.feed(" ещё") + g2.finish())(V.MarkerGuard()) == "см. [[скобки]] и [ ещё")
from bank_audit.research.gptr.citations import StreamRenumberer as _SR
_reg = NS(facts=[NS(id=1, url="u", stance="declared", to_ui=lambda: {})]); _r = _SR(_reg); _g = V.MarkerGuard()
check("маркер, собранный из якоря через перенумеровщик, не рождается", "[[VIZ:0]]" not in _g.feed(_r.feed("[[[f:99999]VIZ:0]]")) + _g.feed(_r.finish()) + _g.finish())
check("resanitize режет тысячи svg", V.resanitize('<div class="viz">' + '<svg viewBox="0 0 1 1"></svg>' * 20000 + '</div>') == "")
V.LOGO_DIR = "/nonexistent"; V._LOGO_CACHE.clear()
check("кэш логотипов не запоминает отсутствие", V._official_logo("vtb") == "" and "vtb" not in V._LOGO_CACHE)

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
