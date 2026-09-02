"""Досье: отчёт по разделам, каждый со своими фактами целиком."""
import inspect
import pathlib
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

ok = fail = 0


def check(name, cond):
    global ok, fail
    if cond:
        ok += 1; print(f"  ✓ {name}")
    else:
        fail += 1; print(f"  ✗ {name}")


from bank_audit.research.gptr import dossier as D, facts as F      # noqa: E402

def fact(i, subj, attr, val, stance, url="https://x/", date="", verbatim=None):
    return F.Fact(id=i, subject=subj, attribute=attr, value=val, unit="",
                  verbatim=verbatim or f"цитата {i}", url=url, stance=stance, date=date)

reg = F.FactRegistry()
# Сбер: ставка заявлена дважды разными значениями (конфликт), и 30 жалоб
reg.facts += [fact(1, "sberbank", "ставка", "1%", "declared", "https://sber/a"),
              fact(2, "sberbank", "ставка", "1,95%", "declared", "https://agg/b"),
              fact(3, "sberbank", "лимит", "300 000 ₽", "declared")]
reg.facts += [fact(100 + i, "sberbank", "жалобы", f"жалоба {i}", "observed",
                   date=f"2026-08-{(i % 28) + 1:02d}") for i in range(30)]
# дочка
reg.facts += [fact(4, "evotor", "терминал", "9 900 ₽", "declared")]
# конкурент: 12 одинаковых значений ставки с разных страниц + 3 разных
reg.facts += [fact(200 + i, "vtb", "ставка", "1,3%", "declared", f"https://vtb/{i}")
              for i in range(12)]
reg.facts += [fact(300 + i, "vtb", "ставка", f"1,{5+i}%", "declared") for i in range(3)]
reg.facts += [fact(400 + i, "vtb", "жалобы", f"жалоба втб {i}", "observed") for i in range(5)]
# норма и общий факт
reg.facts += [fact(500, "", "раскрытие", "обязателен", "regulatory"),
              fact(501, "sberbank", "раскрытие", "в рамке", "declared"),
              fact(502, "", "ставка", "НДС 22%", "declared")]

plan = NS(subjects=["sberbank", "vtb", "evotor"], subsidiaries=["evotor"], anchor="sberbank",
          subject_labels={"sberbank": "Сбербанк", "vtb": "ВТБ", "evotor": "Эвотор"},
          intent_summary="", output_sections=[])

print("\n— точка отсчёта и периметр —")
check("якорь — Сбер", D.anchor_of(plan) == "sberbank")
check("дочка входит в периметр", D.anchor_family(plan) == {"sberbank", "evotor"})
check("без Сбера и без явного якоря точки отсчёта НЕТ",
      D.anchor_of(NS(subjects=["vtb"], subsidiaries=[], anchor="")) == "")
check("кондуктор задаёт точку зрения явно: Домклик против площадок",
      D.anchor_of(NS(subjects=["domclick", "cian", "avito"], subsidiaries=[],
                     anchor="domclick")) == "domclick")
check("явный якорь вне субъектов не принимается",
      D.anchor_of(NS(subjects=["sberbank", "vtb"], subsidiaries=[], anchor="cian")) == "sberbank")

print("\n— карта условий: Сбер, дочки, общее, нормы; без конкурентов и жалоб —")
c = D.facts_for("conditions", reg, plan)
subjects = {f.subject for f in c}
check("конкурента нет", "vtb" not in subjects)
check("дочка есть", "evotor" in subjects)
check("общий факт есть", "" in subjects)
check("норма есть", any(f.stance == "regulatory" for f in c))
check("жалоб нет", not any(f.stance == "observed" for f in c))
check("оба значения ставки Сбера сохранены", {f.value for f in c if f.attribute == "ставка" and f.subject == "sberbank"} == {"1%", "1,95%"})

print("\n— Сбер против рынка: потолок бережёт РАЗНЫЕ значения —")
m = D.facts_for("market", reg, plan)
vtb_rate = [f for f in m if f.subject == "vtb" and f.attribute == "ставка"]
check("на ячейку не больше потолка", len(vtb_rate) <= D._MARKET_PER_CELL)
check("три разных значения ВТБ все на месте",
      {"1,5%", "1,6%", "1,7%"} <= {f.value for f in vtb_rate})
check("повторы 1,3% не вытеснили разные", sum(1 for f in vtb_rate if f.value == "1,3%") < 12)
check("наблюдаемых нет", not any(f.stance == "observed" for f in m))

print("\n— голос клиента: Сбер первым, до 25 на объект —")
v = D.facts_for("voice", reg, plan)
check("только наблюдаемые", all(f.stance == "observed" for f in v))
check("Сбер идёт первым", v[0].subject == "sberbank")
check("потолок на объект соблюдён", sum(1 for f in v if f.subject == "sberbank") == D._VOICE_PER_SUBJECT)
check("это больше прежних шести", D._VOICE_PER_SUBJECT > 6)
check("конкурент тоже есть", any(f.subject == "vtb" for f in v))

print("\n— норма: рядом заявленное Сбером по той же характеристике —")
r = D.facts_for("regulatory", reg, plan)
check("норма и заявленное Сбера вместе",
      {f.id for f in r} == {500, 501})
check("ставка Сбера (не нормируется) не подмешана", 1 not in {f.id for f in r})

print("\n— расхождения: только ячейки с разными значениями —")
k = D.facts_for("conflicts", reg, plan)
cells = {(f.subject, f.attribute) for f in k}
check("ставка Сбера — конфликт (1% против 1,95%)", ("sberbank", "ставка") in cells)
check("лимит Сбера — не конфликт", ("sberbank", "лимит") not in cells)
check("ставка ВТБ — конфликт", ("vtb", "ставка") in cells)

print("\n— промпты: точка отсчёта, суждение, якоря —")
labels = plan.subject_labels
for key in D.WRITING_ORDER:
    p = D.section_prompt(key, plan, "вопрос", labels, facts_text="[f:1] …",
                         prior_text="тело", gaps_text="пробел")
    check(f"{key}: точка отсчёта названа", "ТОЧКА ОТСЧЁТА — Сбербанк" in p)
    check(f"{key}: дочка названа", "Эвотор" in p)
    check(f"{key}: требует вывода", "Вывод:" in p)
    check(f"{key}: требует якорей [f:N]", "[f:12]" in p)
p_checks = D.section_prompt("checks", plan, "в", labels, facts_text="i", prior_text="ТЕЛО-МАРКЕР")
check("«что проверять» получает тело", "ТЕЛО-МАРКЕР" in p_checks)
check("«что проверять» требует действий, а не «изучить»", "Действие, а не «изучить»" in p_checks)
check("«что проверять» переносит чужую боль на себя", "Болит у других" in p_checks)
p_voice = D.section_prompt("voice", plan, "в", labels, facts_text="i")
check("голос клиента требует ДОСЛОВНЫХ цитат", "ДОСЛОВНЫМИ цитатами" in p_voice)
check("голос клиента умеет топ-N", "топ-N тем" in p_voice)
p_reg = D.section_prompt("regulatory", plan, "в", labels, facts_text="i")
check("норма: режим документа целиком", "выпиши из фактов документ полностью" in p_reg)
p_any = D.section_prompt("conditions", plan, "в", labels, facts_text="i")
check("заказ аудитора уважается", "ЗАКАЗ АУДИТОРА" in p_any and "устаревшие" in p_any)
p_market = D.section_prompt("market", plan, "в", labels, facts_text="i")
check("рынок: лучше/хуже/паритет", "лучше, хуже, паритет" in p_market)
check("рынок: честный отказ от ранжирования", "сопоставимой базы нет" in p_market)

print("\n— заголовки и промпт без своей стороны —")
t = D.titles(plan)
check("заголовок рынка под точку отсчёта", t["market"] == "Сбербанк против рынка")
check("карта условий называет дочек", t["conditions"] == "Карта условий: Сбербанк и дочерние компании")
no_anchor = NS(subjects=["fpk", "eos", "pkb"], subsidiaries=[], anchor="",
               subject_labels={"fpk": "ФПК", "eos": "ЭОС", "pkb": "ПКБ"}, intent_summary="")
t0 = D.titles(no_anchor)
check("без своей стороны — «Сравнение объектов», а не «Сбер против рынка»",
      t0["market"] == "Сравнение объектов")
p0 = D.section_prompt("market", no_anchor, "рейтинг коллекторов", no_anchor.subject_labels, facts_text="i")
check("без своей стороны Сбер не притянут", "ТОЧКИ ОТСЧЁТА НЕТ" in p0 and "ТОЧКА ОТСЧЁТА —" not in p0)
p1 = D.section_prompt("summary", plan, "стоит ли реагировать?", labels, facts_text="i", prior_text="т")
check("резюме отвечает на вопрос-решение прямо", "стоит ли" in p1 and "да или нет" in p1)
p2 = D.section_prompt("checks", plan, "в", labels, facts_text="i", prior_text="т")
check("план проверки может стать главным разделом", "этот раздел главный" in p2)
p3 = D.section_prompt("market", plan, "в", labels, facts_text="i")
check("рынок: топ-N и позиция точки отсчёта", "N-й из M" in p3)
check("честность словами аудиторов", "не додумывай, честно укажи" in p3)
check("«не кредитует» вместо молчания", "не кредитует" in p3)

print("\n— порядок: пишем материал → суждение, читаем суждение → материал —")
check("резюме пишется последним", D.WRITING_ORDER[-1] == "summary")
check("резюме читается первым", D.READING_ORDER[0] == "summary")
check("«что проверять» перед резюме в написании и после него в чтении",
      D.WRITING_ORDER.index("checks") < D.WRITING_ORDER.index("summary")
      and D.READING_ORDER.index("checks") == 1)
check("все разделы чтения есть в написании", set(D.READING_ORDER) == set(D.WRITING_ORDER))
check("у каждого раздела заголовок по-русски", all(k in D.TITLES for k in D.READING_ORDER))
check("поток берёт заголовки под точку отсчёта", "al_dossier.titles(plan)" in pathlib.Path("src/bank_audit/research/gptr/stream.py").read_text(encoding="utf-8"))

print("\n— интеграция с потоком —")
stream_src = (pathlib.Path("src/bank_audit/research/gptr/stream.py")).read_text(encoding="utf-8")
check("поток зовёт досье", "al_dossier.write_dossier(" in stream_src)
check("оглавление уходит до текста", '"type": "outline"' in stream_src)
check("резюме уходит событием lead", '"type": "lead"' in stream_src)
check("итоговый текст собирается в порядке чтения", 'report = lead_text + "".join(body_parts)' in stream_src)
check("хвост тела сбрасывается ДО резюме", "tail = renum.finish()" in stream_src)
check("старый писатель одним куском не вызывается", "engine_stream_report(" not in stream_src.split("write_dossier")[1])
jsx = pathlib.Path("src/bank_audit/web/static/app.jsx").read_text(encoding="utf-8")
check("фронт вставляет lead наверх", 'data.type==="lead"' in jsx and "data.chunk+(last.text||" in jsx)
eng = inspect.getsource(sys.modules["bank_audit.research.gptr.engine"].stream_report)
check("движок принимает сырой промпт", "raw_prompt" in eng)
rv = pathlib.Path("src/bank_audit/research/gptr/reviews.py").read_text(encoding="utf-8")
check("отзывов на объект теперь 20", "_PER_SUBJECT = 20" in rv)
cond = pathlib.Path("src/bank_audit/research/v2/conductor.py").read_text(encoding="utf-8")
check("кондуктор знает про дочерние компании", '"subsidiaries"' in cond and "subsidiaries=" in cond)

# ---- живой прогон 02.09: заголовки дважды, оговорка про конкурентов, повтор сравнения, lead не сохранялся ----
print("\n— после живого прогона —")
import asyncio as _aio
_src = pathlib.Path(D.__file__).read_text(encoding="utf-8")
check("правило формы: начинать с абзаца, без капса", "начинай сразу с абзаца" in _src and "НЕ НАЧИНАЙ" not in _src and "только подзаголовки ###" in _src)
check("якорь только в квадратных скобках", "(f:N) или f:N без скобок" in _src)
check("карта условий не пишет оговорок про конкурентов", "оговорок об их" in _src)
check("расхождения не повторяют сравнение", "не повторяй его, сводных таблиц" in _src)

async def _gen(*xs):
    for x in xs:
        yield x
async def _collect(*xs, title=""):
    return "".join([p async for p in D._without_heading(_gen(*xs), title)])
_run = lambda *xs, **kw: _aio.run(_collect(*xs, **kw))
check("заголовок ## в начале срезается", _run("## Голос клиента\n\nТекст [f:1]") == "Текст [f:1]")
check("заголовок # в начале срезается", _run("# Голос", " клиента\n", "Текст") == "Текст")
check("второй заголовок остаётся — это содержание", _run("## А\n\n### Тема 1\n") == "### Тема 1\n")
check("текст без заголовка не трогается", _run("Раздел ", "фиксирует…") == "Раздел фиксирует…")
check("подзаголовок ### в начале не срезается", _run("### Тема 1\nтекст") == "### Тема 1\nтекст")
check("пустые куски в начале не ломают", _run("", "\n", "## X\nY") == "Y")
check("write_dossier пишет разделы через срез", _src.count("_without_heading(_stream_section(") == 2)
check("срез получает название раздела", _src.count("_without_heading(_stream_section(client, model, prompt), ttl[key])") == 2)
check("«##» первым куском без пробела — всё равно срезается", _run("##", " Голос клиента\n", "\nТекст") == "Текст")
check("заголовок с названием раздела срезается дважды", _run("# Голос клиента\n\n## Голос клиента\n\n### Тема 1\n", title="Голос клиента") == "### Тема 1\n")
check("чужой заголовок после названия раздела остаётся содержанием", _run("## Сбер против рынка\n\n## Предмет сравнения\n\nТекст", title="Сбербанк против рынка") == "## Предмет сравнения\n\nТекст")
check("после чужого заголовка второй остаётся", _run("## Предмет сравнения\n\n## Точка отсчёта\n\nТекст", title="Голос клиента") == "## Точка отсчёта\n\nТекст")
check("похожее название узнаётся по словам", D._same_title("## Сбер против рынка: эквайринг", "Сбербанк против рынка") and not D._same_title("## Предмет сравнения", "Сбербанк против рынка"))
check("текст, начинающийся с решётки в середине, не теряется", _run("Абзац\n## Заголовок\nещё") == "Абзац\n## Заголовок\nещё")
_app = (pathlib.Path(D.__file__).resolve().parents[2] / "web/app.py").read_text(encoding="utf-8")
check("сохранение отчёта учитывает lead", 'elif t == "lead" and data.get("chunk")' in _app
      and '"".join(lead_parts) + "".join(parts)' in _app)

print("\n— факты-заглушки не доходят до писателя —")
from types import SimpleNamespace as _NS
_mk = lambda v, q: _NS(verbatim=q, value=v)
check("пустая цитата + «не указано» — не факт", not D.substantive(_mk("не указано", "")))
check("пустая цитата + «нет данных» — не факт", not D.substantive(_mk("Нет данных.", "")))
check("пустая цитата + прочерк — не факт", not D.substantive(_mk("—", "")))
check("пустая цитата, но значение есть — факт", D.substantive(_mk("1,25%", "")))
check("цитата есть — факт, даже если значение странное", D.substantive(_mk("не указано", "ставка не указана на сайте")))
check("facts_for фильтрует через substantive", "if substantive(f)]" in _src.replace("\n", "") or "if substantive(f)]" in pathlib.Path(D.__file__).read_text(encoding="utf-8"))

print("\n— оглавление и дочки —")
_plan = _NS(subjects=["domclick"], anchor="domclick", subsidiaries=["domclick"], subject_labels={"domclick": "Домклик"})
check("якорь не считается собственной дочкой", D.subsidiaries_of(_plan) == [])
check("заголовок без «и дочерние компании»", D.titles(_plan)["conditions"] == "Карта условий: Домклик")
_plan2 = _NS(subjects=["sberbank"], anchor="sberbank", subsidiaries=["domclick"], subject_labels={"sberbank": "Сбербанк"})
check("настоящая дочка остаётся", D.subsidiaries_of(_plan2) == ["domclick"])
_reg = _NS(facts=[_NS(id=1, subject="domclick", attribute="срок", value="1 день", unit="", verbatim="за 1 день", url="u", stance="regulatory", date="", support="", confidence=1.0)], by_cell=lambda: {})
_ol = D.outline(_plan, _reg)
check("оглавление всегда начинается с резюме и плана", _ol[:2] == [D.TITLES["summary"], D.TITLES["checks"]])
check("раздел без фактов в оглавление не попадает", "Домклик против рынка" not in _ol and "Голос клиента" not in _ol)
check("раздел с фактами — попадает", "Карта условий: Домклик" in _ol)
check("поток берёт оглавление из outline()", "al_dossier.outline(plan, registry)" in pathlib.Path(D.__file__).with_name("stream.py").read_text(encoding="utf-8"))

print("\n— отзывы о дочке, которой нет в корпусе как банка —")
from bank_audit.research.gptr import reviews as RV
from bank_audit.rag import bankiru_reviews as _br
_calls = []
def _multi(query, *, banks, k_per, since_days=None, **kw):
    _calls.append(("multi", banks)); return {"Сбербанк": [{"text": "долго ждал карту", "url": "u1", "date": "2026-05-01"}]}
def _single(query, *, k, since_days=None, **kw):
    _calls.append(("single", query, k))
    return [{"text": "Домклик обещал сделку за день, ждал неделю", "url": "u2", "date": "2026-06-01"},
            {"text": "просто жалоба на банк без названия сервиса", "url": "u3", "date": "2026-06-02"}]
_orig = (_br.search_reviews_multi, _br.search_reviews, _br.is_available)
_br.search_reviews_multi, _br.search_reviews, _br.is_available = _multi, _single, lambda: True
try:
    _plan = _NS(subjects=["sberbank", "domclick"], subject_labels={"sberbank": "Сбербанк", "domclick": "Домклик"}, product="сделка")
    _recs = RV.collect(_plan, _NS(observed="расхождение рекламы и услуги"), per_subject=5)
finally:
    _br.search_reviews_multi, _br.search_reviews, _br.is_available = _orig
_by = {}
for r in _recs: _by.setdefault(r["subject"], []).append(r)
check("банк из корпуса найден по слагу", len(_by.get("sberbank", [])) == 1)
check("дочка без слага найдена по названию", len(_by.get("domclick", [])) == 1)
check("оставлен только текст с названием дочки", _by["domclick"][0]["url"] == "u2")
check("запасной поиск идёт с названием в запросе", any(c[0] == "single" and c[1].startswith("Домклик") for c in _calls))
check("запасной поиск не зовётся для покрытых объектов", sum(1 for c in _calls if c[0] == "single") == 1)
check("отзыв дочки подписан её именем", _by["domclick"][0]["bank"] == "Домклик")
print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
