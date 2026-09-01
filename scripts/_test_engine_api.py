"""Дымовая проверка движка: всё ли на месте, что зовут друг у друга.

ЗАЧЕМ. Правка в compat.py вырезала `probe_models` вместе с соседним блоком —
компиляция прошла, тесты прошли, а прод упал на первом же прогоне с
«no attribute probe_models». Отсутствующий атрибут виден только при вызове,
поэтому здесь перечислено то, что модули ждут друг от друга.

Запуск:  .venv/bin/python scripts/_test_engine_api.py
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ok = fail = 0


def chk(name, cond):
    global ok, fail
    ok += bool(cond); fail += not cond
    print(("  ✓ " if cond else "  ✗ ") + name)


print("модули движка импортируются")
from bank_audit.research.gptr import (  # noqa: E402
    citations, compat, critic, engine, facts, gaps, planner, retriever,
    reviews, runstate, scraper, stream, verify)
chk("все 13 модулей", True)

# Что каждый модуль обязан предоставлять СВОИМ вызывающим.
CONTRACT = {
    compat:    ["install", "probe_models", "_rejected_param"],
    critic:    ["review", "Verdict", "EXACT", "CLOSE", "UNSUPPORTED"],
    engine:    ["install", "report_prompt", "_role_prompt", "stream_report"],
    facts:     ["Contract", "FactRegistry", "plan_attributes", "build_registry",
                "extract_into", "extract_while_collecting", "select_pages",
                "stance_for", "verbatim_found"],
    citations: ["StreamRenumberer", "renumber", "unanchored_claims"],
    gaps:      ["collect", "render"],
    planner:   ["install", "plan_to_subqueries"],
    reviews:   ["collect", "as_pages", "subject_hints", "stamp_dates"],
    retriever: ["FleetSearch"],
    scraper:   ["install", "AuditLensScraper"],
    runstate:  ["new_run", "bind", "current"],
    verify:    ["verify_report"],
    stream:    ["stream_deep_research_gptr"],
}
print("публичные точки входа на месте")
for mod, names in CONTRACT.items():
    missing = [n for n in names if not hasattr(mod, n)]
    chk(f"{mod.__name__.rsplit('.', 1)[-1]}: {len(names)} точек", not missing)
    for n in missing:
        print(f"      ↳ НЕТ: {n}")

print("вызовы между модулями сходятся по сигнатурам")
sig = inspect.signature(facts.build_registry).parameters
chk("build_registry принимает reg/already (опережающее извлечение)",
    {"reg", "already", "keep_pages", "subject_hints"} <= set(sig))
chk("stream_report — асинхронный генератор",
    inspect.isasyncgenfunction(engine.stream_report))
chk("extract_while_collecting — корутина",
    inspect.iscoroutinefunction(facts.extract_while_collecting))
chk("stream_deep_research_gptr — асинхронный генератор",
    inspect.isasyncgenfunction(stream.stream_deep_research_gptr))

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
