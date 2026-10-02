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

# --- 7. «Unknown parameter: 'thinking'» — тоже отказ параметра
check("разбор: thinking", L._rejected_param(RuntimeError(
    "Error code: 400 - {'error': {'message': \"Unknown parameter: 'thinking'.\", "
    "'param': 'thinking', 'code': 'unknown_parameter'}}")) == "thinking")


# --- 8. слой фактов: отказ модели от thinking запоминается, повтора на второй
#        странице нет (замер 21.09: 112 лишних 400-х на отчёт)
from bank_audit.research.gptr.facts import call_model  # noqa: E402

class FakeClient:
    def __init__(self, fn):
        self.calls = []
        create = self._make(fn)
        self.chat = type("Chat", (), {})()
        self.chat.completions = type("Comp", (), {})()
        self.chat.completions.create = create

    def _make(self, fn):
        async def create(**kw):
            self.calls.append(dict(kw))
            return await fn(**kw)
        return create

async def no_thinking(**kw):
    if (kw.get("extra_body") or {}).get("thinking"):
        raise RuntimeError("Error code: 400 - {'error': {'message': \"Unknown parameter: 'thinking'.\"}}")
    return Resp("{}")

L._DROP_PARAMS.clear()
fc = FakeClient(no_thinking)
def page_kw():
    return {"model": "mini-x", "temperature": 0.0, "extra_body": {"thinking": {"type": "disabled"}}}
asyncio.run(call_model(fc, "mini-x", page_kw()))
asyncio.run(call_model(fc, "mini-x", page_kw()))
check("факты: первая страница — отказ и повтор, вторая — сразу", len(fc.calls) == 3)
check("факты: вторая страница без thinking", not (fc.calls[2].get("extra_body") or {}).get("thinking"))
check("факты: temperature на месте", fc.calls[2].get("temperature") == 0.0)
check("факты: отказ запомнен за моделью", L._DROP_PARAMS.get("mini-x") == {"thinking"})

# отказ без имени параметра лечится вслепую, но НЕ запоминается
async def blind(**kw):
    if kw.get("extra_body"):
        raise RuntimeError("upstream error")
    return Resp("{}")

L._DROP_PARAMS.clear()
fc = FakeClient(blind)
asyncio.run(call_model(fc, "blind-x", page_kw()))
check("слепой повтор без extra_body", len(fc.calls) == 2 and "extra_body" not in fc.calls[1])
check("слепой повтор не запоминается", "blind-x" not in L._DROP_PARAMS)

async def dead2(**kw):
    raise RuntimeError("upstream error")
fc = FakeClient(dead2)
try:
    asyncio.run(call_model(fc, "dead-x", page_kw())); raised = False
except RuntimeError:
    raised = True
check("настоящая ошибка пробрасывается после слепого повтора", raised and len(fc.calls) == 2)


# --- 9. писатель досье: temperature отвергнута один раз, разделы дальше идут
#        без лишнего вызова
from bank_audit.research.gptr import engine as E  # noqa: E402

class FakeStream:
    def __init__(self, pieces):
        self._it = iter(pieces)
    def __aiter__(self):
        return self
    async def __anext__(self):
        try:
            text = next(self._it)
        except StopIteration:
            raise StopAsyncIteration
        delta = type("D", (), {"content": text})()
        return type("P", (), {"choices": [type("C", (), {"delta": delta})()]})()

async def opus_no_temp(**kw):
    if "temperature" in kw:
        raise RuntimeError("Error code: 400 - {'error': {'message': '`temperature` is deprecated for this model.'}}")
    return FakeStream(["раз", "дел"])

async def write_section(client):
    return "".join([p async for p in E.stream_report(
        client, "opus-x", question="q", plan=None, context="", raw_prompt="раздел")])

L._DROP_PARAMS.clear()
fc = FakeClient(opus_no_temp)
t1 = asyncio.run(write_section(fc))
n_first = len(fc.calls)
t2 = asyncio.run(write_section(fc))
check("писатель: текст первого раздела собран", t1 == "раздел")
check("писатель: первый раздел — отказ и повтор", n_first == 2)
check("писатель: второй раздел — один вызов", len(fc.calls) - n_first == 1)
check("писатель: второй раздел без temperature", "temperature" not in fc.calls[-1])
check("писатель: текст второго раздела собран", t2 == "раздел")


# --- 10. проба compat на старте прогона кормит ту же память
#         (пакет движка стоит только в образе — без него проверка пропускается)
import importlib.util  # noqa: E402
if importlib.util.find_spec("gpt_researcher") is None:
    print("  пропуск 10: gpt_researcher не установлен локально")
else:
    import httpx  # noqa: E402
    from bank_audit.research.gptr import compat  # noqa: E402

    L._DROP_PARAMS.clear()
    _orig_post = httpx.post
    httpx.post = lambda *a, **kw: type("R", (), {"status_code": 400,
                                                  "text": "`temperature` is deprecated for this model."})()
    try:
        compat.probe_models(["opus-probe"], base_url="http://x/v1", api_key="k")
    finally:
        httpx.post = _orig_post
    check("проба compat записала отказ в общую память", L._DROP_PARAMS.get("opus-probe") == {"temperature"})
    kw = {"model": "opus-probe", "temperature": 0.3}
    L.drop_known_rejected("opus-probe", kw)
    check("после пробы temperature снимается до вызова", "temperature" not in kw)

print(f"\n{ok} проверок пройдено, провалов: {fail}")
sys.exit(1 if fail else 0)
