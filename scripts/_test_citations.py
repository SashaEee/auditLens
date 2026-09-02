"""Перенумератор якорей: форма записи, повторы, границы кусков потока."""
import pathlib, sys
from types import SimpleNamespace as NS
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from bank_audit.research.gptr.citations import StreamRenumberer   # noqa: E402

ok = fail = 0
def check(name, cond):
    global ok, fail
    ok, fail = ok + bool(cond), fail + (not cond); print(("  ✓ " if cond else "  ✗ ") + name)

def fact(i, url): return NS(id=i, url=url, stance="declared", to_ui=lambda: {"id": i})
reg = NS(facts=[fact(1, "a"), fact(2, "a"), fact(3, "b"), fact(4, "c")])

def run(*chunks):
    r = StreamRenumberer(reg); return "".join(r.feed(c) for c in chunks) + r.finish()

print("— форма записи —")
check("[f:N] → [n]", run("x [f:1] y") == "x [1] y")
check("(f:N) тоже якорь", run("x (f:1) y") == "x [1] y")
check("f:N без скобок — не якорь", run("x f:1 y") == "x f:1 y")
check("неизвестный факт исчезает", run("x [f:99] y") == "x  y")

print("— повторы одного источника —")
check("[f:1][f:2] с одной страницы → [1]", run("a [f:1][f:2] b") == "a [1] b")
check("[3][3][3] → [3]", run("[f:1][f:1][f:1] z") == "[1] z")
check("разные источники не склеиваются", run("[f:1][f:3]") == "[1][2]")
check("через пробел тоже склеивается", run("[f:1] [f:2].") == "[1].")
check("нумерация по первому упоминанию", run("[f:4] [f:3] [f:1] [f:3]") == "[1] [2] [3] [2]")

print("— границы кусков —")
check("якорь, разорванный между кусками", run("см. [f:", "1] и") == "см. [1] и")
check("(f: разорванный между кусками", run("см. (f:", "1) и") == "см. [1] и")
check("повтор через границу куска склеивается", run("a [f:1]", "[f:2] b") == "a [1] b")
check("граница внутри второго якоря — тоже склеивается", run("a [f:1][f:", "2] b") == "a [1] b")
check("три якоря через две границы", run("x [f:1]", "[f:2][f:", "1] y") == "x [1] y")
check("цепочка разных источников через границу не теряется", run("x [f:1][f:", "3] y") == "x [1][2] y")
check("текст после якоря отдаётся сразу", run("см. [f:1] далее", " текст") == "см. [1] далее текст")
check("хвост отдаётся в finish", run("текст [f:1]") == "текст [1]")
r = StreamRenumberer(reg); r.feed("a [f:1] b [f:3]"); r.feed(" c")
check("итоговый text равен выдаче", r.text + r.finish() == "a [1] b [2] c")
check("источники по порядку", [x["url"] for x in r.sources()] == ["a", "b"])

print(f"\nитого: {ok} ок, {fail} с ошибкой"); sys.exit(1 if fail else 0)
