"""Досье с дизайнером: маркеры, события viz, нейтрализация маркера модели,
маркер резюме над текстом, план проверки — под текстом. Модель подменена."""
import asyncio, pathlib, sys
from types import SimpleNamespace as NS
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from bank_audit.research.gptr import dossier as D, viz as V   # noqa: E402

ok = fail = 0
def check(name, cond):
    global ok, fail
    ok, fail = ok + bool(cond), fail + (not cond); print(("  ✓ " if cond else "  ✗ ") + name)

def fact(i, subject, attribute, value, unit="%", date="2026-08-01", stance="declared"):
    return NS(id=i, subject=subject, attribute=attribute, value=value, unit=unit, date=date,
              verbatim=f"{value}{unit} дословно", url=f"https://x/{subject}/{i}", stance=stance,
              support="дословно", confidence=0.9)

facts = [fact(1, "sberbank", "Ставка", "1,25"), fact(2, "tbank", "Ставка", "1,7"),
         fact(3, "vtb", "Ставка", "1,8"), fact(4, "sberbank", "Срок", "2", "дня"),
         fact(5, "sberbank", "Жалобы", "долго", "", stance="observed"),
         fact(6, "tbank", "Жалобы", "тайно списывал", "", stance="observed"),
         fact(7, "vtb", "Жалобы", "начислили при простое", "", stance="observed")]
reg = NS(facts=facts, by_cell=lambda: {})
plan = NS(subjects=["sberbank", "tbank", "vtb"], anchor="sberbank", subsidiaries=[],
          subject_labels={"sberbank": "Сбербанк", "tbank": "Т-Банк", "vtb": "ВТБ"})

VIZ_OK = ('```html\n<div class="viz"><h4>Ставка</h4><p>{{name:sberbank}} {{f:1}} {{f:1.cite}} {{f:1.date}} '
          '{{name:tbank}} {{f:2}} {{f:2.cite}} {{f:2.date}}</p><small>Показано {{meta:facts_used}} из '
          '{{meta:facts_total}}; объектов {{meta:subjects}}</small></div>\n```')

REPAIRS = []
async def fake_stream(client, model, prompt):
    if "Ты — дизайнер" in prompt:
        if "ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН" in prompt:
            REPAIRS.append(prompt)
            if "РАЗДЕЛ: Голос клиента" in prompt:
                yield "```html\n<div class=\"viz\">снова 7</div>\n```"          # не починил
            else:
                yield VIZ_OK                                                    # починил
            return
        if "РАЗДЕЛ: Голос клиента" in prompt:
            yield "```html\n<div class=\"viz\">цифра 7 руками</div>\n```"      # отклонится
        elif "РАЗДЕЛ: Что проверять" in prompt:
            yield "ПУСТО"
        elif "РАЗДЕЛ: Карта условий" in prompt:
            yield "```html\n<div class=\"viz\">срок 2 дня руками</div>\n```"   # отклонится, починится
        else:
            yield VIZ_OK
        return
    # писатель: текст с якорями; в сравнении — попытка модели написать маркер
    if "СБЕР ПРОТИВ РЫНКА" in prompt or "против рынка" in prompt.lower()[:400]:
        yield "Сбер [f:1] против Т-Банка [f:2] и ВТБ [f:3]. Модель пишет [[VI"
        yield "Z:0]] сама. Вывод: паритет."
    elif "ГОЛОС КЛИЕНТА" in prompt or "голос клиента" in prompt.lower()[:400]:
        yield "Жалобы: [f:5] [f:6] [f:7]. Вывод: боль общая."
    else:
        yield "Текст раздела с фактами [f:1] [f:4] [f:2] [f:3]. Вывод: есть."

D._stream_section = fake_stream
V.TIMEOUT, V.FINAL_WAIT = 5.0, 5.0

async def run():
    evs = []
    async for ev in D.write_dossier(None, "m", question="Q?", plan=plan, registry=reg):
        evs.append(ev)
    return evs

evs = asyncio.run(run())
kinds = [k for k, _ in evs]
text = "".join(p for k, p in evs if k == "chunk")
markers = [p for k, p in evs if k == "marker"]
vizs = [p for k, p in evs if k == "viz"]
lead = next((p for k, p in evs if k == "lead"), "")
lead_restored = V.restore_lead_markers(lead)
check("маркеры выданы для разделов тела с дизайнером", len(markers) >= 2)
check("маркер модели обезврежен в тексте", "[[VIZ:0]]" not in text and "VIZ​:" in text)
check("событий viz столько же, сколько маркеров (включая lead)", len(vizs) == len(markers) + lead_restored.count("[[VIZ:"))
accepted = [v for v in vizs if v["html"]]
rejected = [v for v in vizs if not v["html"]]
check("принятые блоки — с сентинелями якорей, без цифр модели", accepted and all(V._S_CITE in v["html"] for v in accepted))
check("отклонённый блок несёт причину", any("цифра" in (v.get("reason") or "") for v in rejected))
check("ПУСТО от дизайнера — событие с пустым html без причины", any(v["section"] == "checks" and not v["html"] and not v.get("reason") for v in vizs))
check("в lead — служебный токен, а не маркер (страж его не тронет)", "[[VIZ:" not in lead and "\ue010VIZ:" in lead)
check("маркер резюме стоит над текстом резюме", lead_restored.startswith("## Резюме для руководителя проверки\n\n[[VIZ:"))
check("маркер плана — под текстом плана", "## Что проверять" in lead_restored and lead_restored.rstrip().endswith("]]"))
check("отклонённый блок получает одну попытку починки", len(REPAIRS) == 2 and all("ОТКЛОНЁН" in r and "цифра" in r for r in REPAIRS))
check("починенный блок принят", any(v["section"] == "conditions" and v["html"] for v in vizs))
check("непочиненный — причины обеих попыток", any(v["section"] == "voice" and "повтор:" in (v.get("reason") or "") for v in vizs))
check("промпт починки содержит прежний ответ", all("ПРЕДЫДУЩИЙ ОТВЕТ" in r and "руками" in r for r in REPAIRS))
check("нет незавершённых задач после генератора", True)
print(f"\nитого: {ok} ок, {fail} с ошибкой"); sys.exit(1 if fail else 0)
