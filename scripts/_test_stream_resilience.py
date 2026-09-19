"""Перегрузка провайдера не должна ронять отчёт и не должна дублировать текст."""
import asyncio, pathlib, sys, types
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from bank_audit.research.gptr import dossier as D

ok = fail = 0
def check(name, cond):
    global ok, fail
    if cond: ok += 1
    else:
        fail += 1
        print("  ПРОВАЛ:", name)

# --- распознавание временных отказов
temp = ["Overloaded", "Error code: 429 - rate limit", "503 Service Unavailable",
        "Request timed out", "temporarily unavailable"]
perm = ["invalid_request_error: unknown model", "authentication failed",
        "`temperature` is deprecated for this model"]
for t in temp: check(f"временная: {t[:26]}", D._RETRY_ERR.search(t) is not None)
for t in perm: check(f"постоянная: {t[:26]}", D._RETRY_ERR.search(t) is None)

calls = {"n": 0}
def make_stream(fail_times, fail_after_pieces=0):
    async def fake(client, model, *, question, plan, context, raw_prompt):
        calls["n"] += 1
        for i in range(fail_after_pieces):
            yield f"кусок{i} "
        if calls["n"] <= fail_times:
            raise RuntimeError("Overloaded")
        yield "готовый текст"
    return fake

async def collect(gen):
    return [p async for p in gen]

# --- 1. падение ДО первого куска → повтор, текст без дублей
orig = D._stream
D._stream = make_stream(fail_times=1)
calls["n"] = 0
out = asyncio.run(collect(D._stream_section(None, "m", "prompt")))
check("повтор состоялся", calls["n"] == 2)
check("текст получен один раз", "".join(out) == "готовый текст")

# --- 2. падение ПОСЛЕ выдачи куска → повтора нет (иначе дубль)
D._stream = make_stream(fail_times=99, fail_after_pieces=2)
calls["n"] = 0
async def run_fail():
    try:
        await collect(D._stream_section(None, "m", "prompt"))
        return "без ошибки"
    except RuntimeError:
        return "ошибка"
res = asyncio.run(run_fail())
check("ошибка проброшена наверх", res == "ошибка")
check("повтора после выдачи не было", calls["n"] == 1)

# --- 3. постоянная ошибка не повторяется
async def perm_stream(client, model, *, question, plan, context, raw_prompt):
    calls["n"] += 1
    raise RuntimeError("unknown model")
    yield ""
D._stream = perm_stream
calls["n"] = 0
try:
    asyncio.run(collect(D._stream_section(None, "m", "prompt")))
    check("постоянная ошибка пробрасывается", False)
except RuntimeError:
    check("постоянная ошибка пробрасывается", True)
check("постоянную не повторяли", calls["n"] == 1)
D._stream = orig

# --- 4. поток отдаёт написанное при обрыве (проверка по исходнику)
src = pathlib.Path(
    pathlib.Path(D.__file__).parent / "stream.py").read_text()
check("частичный отчёт сохраняется", "Отчёт неполный" in src)
check("пустой случай остался прежним", "Отчёт не сформирован" in src)

print(f"\n{ok} проверок пройдено, провалов: {fail}")
sys.exit(1 if fail else 0)
