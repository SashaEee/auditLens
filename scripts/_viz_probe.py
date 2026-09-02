"""Стенд дизайнера: одна секция готового прогона → сырой ответ модели и вердикт
проверок. Позволяет крутить промпт за минуту вместо девятиминутного прогона.

python scripts/_viz_probe.py /tmp/run6.sse market [checks summary ...]
"""
import asyncio, json, os, re, sys
from types import SimpleNamespace as NS
sys.path.insert(0, "/app/src")
from openai import AsyncOpenAI
from bank_audit.research.gptr import viz as V, dossier as D, engine as E

sse, sections = sys.argv[1], sys.argv[2:] or ["market"]
evs = [json.loads(l[5:]) for l in open(sse, encoding="utf-8") if l.startswith("data:") and l[5:].strip().startswith("{")]
plan_ev = next(e for e in evs if e.get("type") == "plan")
subjects = plan_ev.get("subjects") or []
labels = plan_ev.get("subject_labels") or {s: s for s in subjects}
src = next(e for e in evs if e.get("type") == "sources")
facts = []
for s_ in src["sources"]:
    for f in s_.get("facts") or []:
        facts.append(NS(id=f["id"], subject=f.get("subject") or "", attribute=f.get("attribute") or "", value=f.get("value") or "",
                        unit=f.get("unit") or "", date=f.get("date") or "", verbatim=f.get("verbatim") or "", url=f.get("url") or "",
                        stance=f.get("stance") or "declared", support=f.get("support") or "", confidence=1.0))
by_id = {f.id: f for f in facts}
# номер источника → факты: в тексте отчёта якоря уже [n]; восстановим [f:id] по первому факту источника
n_to_ids = {s_["n"]: [f["id"] for f in (s_.get("facts") or [])] for s_ in src["sources"]}
text = "".join(e.get("chunk", "") for e in evs if e.get("type") == "text")
lead = "".join(e.get("chunk", "") for e in evs if e.get("type") == "lead")
question = next((e.get("question") for e in evs if e.get("question")), "") or os.environ.get("Q", "")
plan = NS(subjects=subjects, anchor=plan_ev.get("anchor") or ("sberbank" if "sberbank" in subjects else ""), subsidiaries=[], subject_labels=labels)
# Заголовки берём из события outline: там они ровно такие, как в тексте.
outline = next((e.get("sections") for e in evs if e.get("type") == "outline"), [])
_KEYWORDS = {"summary": "Резюме", "checks": "Что проверять", "conditions": "Карта условий",
             "market": "против рынка", "voice": "Голос клиента", "regulatory": "Нормативная", "conflicts": "Расхождения"}
ttl = {k: next((t for t in outline if kw in t), D.TITLES[k]) for k, kw in _KEYWORDS.items()}
anchor_label = ttl["conditions"].split(":", 1)[1].strip() if ":" in ttl["conditions"] else ""
if anchor_label and plan.anchor:
    labels[plan.anchor] = anchor_label

def section_text(key):
    body = lead + text
    i = body.find("## " + ttl[key])
    if i < 0: return ""
    j = body.find("\n## ", i + 3)
    return body[i:j if j > 0 else None]

client = AsyncOpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"])
model = (os.getenv("SMART_LLM") or "").split(":", 1)[-1] or (os.getenv("LLM_MODEL_ANALYST") or os.environ["LLM_MODEL_NAME"])

async def complete(prompt):
    return "".join([p async for p in E.stream_report(client, model, question="", plan=None, context="", raw_prompt=prompt)])

async def main():
    for key in sections:
        st = section_text(key)
        # факты раздела — по номерам источников [n] в тексте раздела
        ns = [int(x) for x in re.findall(r"\[(\d{1,3})\]", st)]
        ids = [i for n in dict.fromkeys(ns) for i in n_to_ids.get(n, [])]
        sec_facts = [by_id[i] for i in dict.fromkeys(ids) if i in by_id][:60]
        prompt = V.designer_prompt(section=key, title=ttl[key], question=question, anchor=plan.anchor, labels=labels,
                                   facts_text=D.render_facts(sec_facts, labels), section_text=st, subjects=subjects)
        print(f"\n{'='*30} {key}: фактов {len(sec_facts)}, текст {len(st)} знаков {'='*30}")
        ans = ""
        for attempt in range(3):
            try:
                ans = await complete(prompt); break
            except Exception as e:
                print("попытка", attempt + 1, "—", type(e).__name__, str(e)[:120]); await asyncio.sleep(3)
        print("--- сырой ответ (первые 2500) ---"); print(ans[:2500])
        built = V.build(ans, facts=sec_facts, labels=labels, section=key, subjects=subjects)
        print("--- вердикт ---"); print("принято байт:", len(built.html), "| отклонено:", built.rejected)
        open(f"/tmp/probe_{key}.html", "w").write(ans)
asyncio.run(main())
