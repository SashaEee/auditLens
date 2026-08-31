"""Golden-прогон движка на gpt-researcher — тот же набор, те же проверки.

Сравнивается с scripts/golden_run.py (наш конвейер v2) на одних вопросах и
одних ожиданиях. Проверки, которым нужны наши стадии (сверка чисел), помечаются
«н/д» — они появятся, когда сверка будет навешена поверх отчёта.

ЗАПУСК (в песочнице, где есть и наш код, и gpt-researcher):
  docker cp scripts/golden_gptr.py auditlens-gptr:/tmp/
  docker exec auditlens-gptr python /tmp/golden_gptr.py            # все кейсы
  docker exec auditlens-gptr python /tmp/golden_gptr.py regulatory_psk
"""
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, "/app/src")
logging.basicConfig(level=logging.WARNING)

from bank_audit.research.gptr import compat, planner as our_planner  # noqa: E402
from bank_audit.research.gptr import scraper as al_scraper           # noqa: E402
from bank_audit.research.gptr import verify as al_verify             # noqa: E402
from bank_audit.research.gptr.retriever import FleetSearch           # noqa: E402

compat.install()
compat.probe_models(
    [os.environ[k].split(":", 1)[1]
     for k in ("FAST_LLM", "SMART_LLM", "STRATEGIC_LLM")],
    base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["OPENAI_API_KEY"])

import gpt_researcher.retrievers as _r                    # noqa: E402
import gpt_researcher.retrievers.searx.searx as _rs       # noqa: E402
_r.SearxSearch = FleetSearch
_rs.SearxSearch = FleetSearch
al_scraper.install()

from gpt_researcher import GPTResearcher                   # noqa: E402

OFFICIAL = ("sberbank.ru", "sber.ru", "tbank.ru", "tinkoff.ru", "alfabank.ru",
            "vtb.ru", "gazprombank.ru", "domrfbank.ru", "cbr.ru", "dom.rf")


async def run_case(case: dict) -> dict:
    from openai import AsyncOpenAI
    from bank_audit.research.v2.conductor import plan_research as our_plan

    q = case["question"]
    t0 = time.time()
    client = AsyncOpenAI(api_key=os.environ["LLM_API_KEY"],
                         base_url=os.environ["LLM_BASE_URL"])
    plan = await our_plan(client, os.environ.get("LLM_MODEL_REASONING")
                          or os.environ["LLM_MODEL_NAME"], q)
    our_planner.install(plan, q)
    t_plan = time.time()

    al_scraper.READ_PAGES.clear()      # доказательная база этого кейса
    r = GPTResearcher(query=q, report_type="research_report")
    await r.conduct_research()
    t_res = time.time()
    report = await r.write_report()
    t_end = time.time()

    urls = r.get_source_urls()
    hosts = [urlparse(u).netloc.removeprefix("www.") for u in urls]
    official = sum(1 for h in hosts if any(h.endswith(o) for o in OFFICIAL))

    exp = case.get("expect") or {}
    checks, ok, na = [], True, 0

    def chk(name: str, passed: bool):
        nonlocal ok
        ok = ok and passed
        checks.append(("  ✓ " if passed else "  ✗ ") + name)

    if "min_report_chars" in exp:
        chk(f"отчёт ≥ {exp['min_report_chars']} (есть {len(report)})",
            len(report) >= exp["min_report_chars"])
    for group in exp.get("must_mention_any", []):
        chk("упоминает одно из: " + "/".join(group),
            any(w.lower() in report.lower() for w in group))
    for ph in exp.get("forbid_phrases", []):
        chk(f"НЕ содержит «{ph}»", ph.lower() not in report.lower())
    if exp.get("must_have_table"):
        chk("есть markdown-таблица",
            bool(re.search(r"^\|.+\|\s*$", report, re.M)))
    if "plan_nature" in exp:
        chk(f"план: nature={exp['plan_nature']}",
            (getattr(plan, "question_nature", "") or "") == exp["plan_nature"])
    if "plan_subjects_max" in exp:
        n = len(getattr(plan, "subjects", None) or [])
        chk(f"план: субъектов ≤ {exp['plan_subjects_max']} (есть {n})",
            n <= exp["plan_subjects_max"])
    if "min_years_mentioned" in exp:
        years = set(re.findall(r"\b(20[12]\d)\b", report))
        chk(f"лет ≥ {exp['min_years_mentioned']} ({sorted(years)})",
            len(years) >= exp["min_years_mentioned"])
    if exp.get("honest_gap_ok"):
        gap = "прямого ответа" in report.lower()
        chk("данные добыты ИЛИ пробел признан честно",
            len(report) > 1500 or gap)
    ver = al_verify.verify_report(report)
    if "min_verified_ratio" in exp:
        chk(f"сверено ≥ {exp['min_verified_ratio']:.0%} чисел "
            f"({ver['verified']}/{ver['numeric_checked']})",
            ver["ratio"] >= exp["min_verified_ratio"])

    return {
        "id": case["id"], "класс": case.get("class"), "ok": ok,
        "проверки": checks, "н/д": na,
        "сек_план": round(t_plan - t0, 1),
        "сек_поиск": round(t_res - t_plan, 1),
        "сек_отчёт": round(t_end - t_res, 1),
        "сек_всего": round(t_end - t0, 1),
        "символов": len(report), "источников": len(urls),
        "первоисточников": official,
        "ссылок": report.count("http"),
        "хосты": dict(Counter(hosts).most_common(6)),
        "сверка": ver,
        "отчёт": report,
    }


async def main() -> int:
    data = json.loads(Path("/tmp/golden_set.json").read_text())
    cases = data["cases"]
    only = sys.argv[1] if len(sys.argv) > 1 else None
    if only:
        cases = [c for c in cases if c["id"] == only]
    results = []
    for case in cases:
        print(f"\n=== {case['id']} ({case.get('class')}) ===", flush=True)
        try:
            res = await run_case(case)
        except Exception as e:
            print(f"  ✗ ПАДЕНИЕ: {type(e).__name__}: {e}", flush=True)
            results.append({"id": case["id"], "ok": False, "ошибка": str(e)[:200]})
            continue
        for line in res["проверки"]:
            print(line, flush=True)
        v = res.get("сверка") or {}
        print(f"  [{res['сек_всего']}с | {res['символов']} симв | "
              f"{res['источников']} ист, из них {res['первоисточников']} "
              f"первоисточников | сверено {v.get('verified',0)}/"
              f"{v.get('numeric_checked',0)} чисел]", flush=True)
        results.append(res)
        Path("/tmp/golden_gptr_results.json").write_text(
            json.dumps(results, ensure_ascii=False))
    passed = sum(1 for r in results if r.get("ok"))
    print(f"\nИТОГО: {passed}/{len(results)} кейсов прошли", flush=True)
    return 0 if passed == len(results) else 1


sys.exit(asyncio.run(main()))
