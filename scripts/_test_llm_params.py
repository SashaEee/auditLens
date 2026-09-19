"""Модель, отвергнувшая один параметр, не должна уводить вызов на резерв."""
import asyncio, os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from bank_audit.ai import llm_utils as L

ok = fail = 0
def check(name, cond):
    global ok, fail
    if cond: ok += 1
    else:
        fail += 1
        print("  ПРОВАЛ:", name)

class Resp:
    def __init__(self, text="ответ"):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text, "reasoning_content": ""})()})()]

# --- 1. temperature deprecated → снимаем и повторяем ТОЙ ЖЕ моделью
calls = []
async def fake(*a, **kw):
    calls.append(dict(kw))
    if "temperature" in kw:
        raise RuntimeError("Error code: 400 - {'message': '`temperature` is deprecated for this model.'}")
    return Resp()

L._DROP_PARAMS.clear()
os.environ["LLM_MODEL_FALLBACK"] = "backup-model"
r = asyncio.run(L._resilient_create(fake, "opus-x", (), {"model": "opus-x", "temperature": 0.4}))
check("вызвано дважды", len(calls) == 2)
check("повтор той же моделью", calls[1].get("model") == "opus-x")
check("на резерв не ушли", all(c.get("model") != "backup-model" for c in calls))
check("temperature снята", "temperature" not in calls[1])
check("ответ получен", r.choices[0].message.content == "ответ")
check("запомнили параметр", L._DROP_PARAMS.get("opus-x") == {"temperature"})

# --- 2. следующий вызов той же модели уже без лишнего round-trip
calls.clear()
asyncio.run(L._resilient_create(fake, "opus-x", (), {"model": "opus-x", "temperature": 0.4}))
check("превентивно, один вызов", len(calls) == 1)
check("сразу без temperature", "temperature" not in calls[0])

# --- 3. параметр в extra_body тоже снимается
async def fake_extra(*a, **kw):
    calls.append(dict(kw))
    if (kw.get("extra_body") or {}).get("reasoning_effort"):
        raise RuntimeError("Unsupported parameter: 'reasoning_effort'")
    return Resp()

L._DROP_PARAMS.clear(); calls.clear()
asyncio.run(L._resilient_create(fake_extra, "m2", (), {"model": "m2", "extra_body": {"reasoning_effort": "high"}}))
check("extra_body: повтор", len(calls) == 2)
check("extra_body очищен", not (calls[1].get("extra_body") or {}).get("reasoning_effort"))

# --- 4. настоящая недоступность модели по-прежнему уходит на резерв
async def dead(*a, **kw):
    calls.append(dict(kw))
    if kw.get("model") == "gone":
        raise RuntimeError("model not found")
    return Resp()

L._DROP_PARAMS.clear(); calls.clear()
asyncio.run(L._resilient_create(dead, "gone", (), {"model": "gone", "temperature": 0.4}))
check("резерв всё ещё работает", calls[-1].get("model") == "backup-model")

# --- 5. распознавание формулировок провайдера
cases = [("`temperature` is deprecated for this model.", "temperature"),
         ("Unsupported parameter: 'top_p'", "top_p"),
         ("unknown field `frequency_penalty`", "frequency_penalty"),
         ("rate limit exceeded", None),
         ("The model is currently overloaded", None)]
for text, want in cases:
    check(f"разбор: {text[:32]}", L._rejected_param(RuntimeError(text)) == want)

# --- 6. _fit_kwargs учитывает известные ограничения резерва
L._DROP_PARAMS.clear()
L._DROP_PARAMS["backup-model"] = {"temperature"}
out = L._fit_kwargs("backup-model", {"model": "x", "temperature": 0.4})
check("_fit_kwargs снимает известное", "temperature" not in out)

print(f"\n{ok} проверок пройдено, провалов: {fail}")
sys.exit(1 if fail else 0)
