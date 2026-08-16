"""Golden-прогон конвейера deep-research: регрессия ДО деплоя.

ЗАЧЕМ. Любая правка промпта или стадии уезжала на прод вслепую: сломанную
рамку вопроса или потерянные факты замечали аудиторы волной дизлайков через
дни. Здесь — эталонные вопросы по всем классам таксономии с ДЕТЕРМИНИРОВАННЫМИ
ожиданиями (без LLM-судьи: проверки бесплатные и не флапают).

ЗАПУСК (на проде, внутри контейнера — там env и БД):
  docker cp scripts/golden_run.py auditlens-app:/tmp/
  docker cp scripts/golden_set.json auditlens-app:/tmp/
  docker exec auditlens-app python3 /tmp/golden_run.py            # все кейсы
  docker exec auditlens-app python3 /tmp/golden_run.py regulatory_psk  # один

Выход: по кейсу — PASS/FAIL с расшифровкой каждого ожидания; код возврата 1
при любом FAIL. Полные события пишутся в /tmp/golden_<id>.json — для разбора.
"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "/app/src")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bank_audit import db  # noqa: E402
db.init()


def _events_of(raw_events):
    out = []
    for ev in raw_events:
        r = ev[6:] if isinstance(ev, str) and ev.startswith("data: ") else ev
        try:
            d = json.loads(r) if isinstance(r, str) else r
        except Exception:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


async def run_case(case: dict) -> tuple[bool, list[str]]:
    from bank_audit.research.v2.orchestrator import stream_deep_research_v2
    raw = []
    async for ev in stream_deep_research_v2(case["question"], history=None):
        raw.append(ev)
    evs = _events_of(raw)
    Path(f"/tmp/golden_{case['id']}.json").write_text(
        json.dumps(evs, ensure_ascii=False))

    report = "".join(e.get("chunk", "") for e in evs if e.get("type") == "text")
    plan = next((e for e in evs if e.get("type") == "plan"), {})
    ver = next((e for e in evs if e.get("type") == "verification"), {})
    exp = case.get("expect") or {}
    checks: list[str] = []
    ok = True

    def chk(name: str, passed: bool):
        nonlocal ok
        ok = ok and passed
        checks.append(("  ✓ " if passed else "  ✗ ") + name)

    if "min_report_chars" in exp:
        chk(f"отчёт ≥ {exp['min_report_chars']} символов (есть {len(report)})",
            len(report) >= exp["min_report_chars"])
    for group in exp.get("must_mention_any", []):
        chk("упоминает одно из: " + "/".join(group),
            any(w.lower() in report.lower() for w in group))
    for ph in exp.get("forbid_phrases", []):
        chk(f"НЕ содержит «{ph}»", ph.lower() not in report.lower())
    if exp.get("must_have_table"):
        chk("есть markdown-таблица", bool(re.search(r"^\|.+\|\s*$", report, re.M)))
    if "plan_nature" in exp:
        chk(f"план: nature={exp['plan_nature']}",
            (plan.get("question_nature") or plan.get("nature") or "")
            == exp["plan_nature"])
    if "plan_subjects_max" in exp:
        n_subj = len(plan.get("subjects") or [])
        chk(f"план: субъектов ≤ {exp['plan_subjects_max']} (есть {n_subj})",
            n_subj <= exp["plan_subjects_max"])
    if "min_verified_ratio" in exp:
        checked = int(ver.get("numeric_checked") or 0)
        verified = int(ver.get("verified") or 0)
        ratio = (verified / checked) if checked else 1.0
        chk(f"сверено ≥ {exp['min_verified_ratio']:.0%} чисел "
            f"({verified}/{checked})", ratio >= exp["min_verified_ratio"])
    if "min_years_mentioned" in exp:
        years = set(re.findall(r"\b(20[12]\d)\b", report))
        chk(f"упомянуто ≥ {exp['min_years_mentioned']} разных лет "
            f"({sorted(years)})", len(years) >= exp["min_years_mentioned"])
    if exp.get("honest_gap_ok"):
        # Для вопросов, где данных может не быть: честное признание пробела
        # (первой строкой или в unanswered) засчитывается как правильный ответ.
        gap_honest = ("прямого ответа" in report.lower()
                      or bool(ver.get("unanswered")))
        chk("данные добыты ИЛИ пробел признан честно",
            len(report) > 1500 or gap_honest)
    return ok, checks


async def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    cases = json.loads(
        (Path("/tmp/golden_set.json") if Path("/tmp/golden_set.json").exists()
         else Path(__file__).parent / "golden_set.json").read_text())["cases"]
    if only:
        cases = [c for c in cases if c["id"] == only]
    if not cases:
        print(f"кейс «{only}» не найден")
        return 2
    failed = 0
    for c in cases:
        print(f"\n═══ {c['id']} ({c['class']}) ═══")
        print(f"    {c['question'][:90]}")
        try:
            ok, checks = await run_case(c)
        except Exception as e:  # noqa: BLE001
            ok, checks = False, [f"  ✗ прогон упал: {e}"]
        print("\n".join(checks))
        print("    →", "PASS" if ok else "FAIL")
        failed += 0 if ok else 1
    print(f"\nитого: {len(cases) - failed} PASS, {failed} FAIL из {len(cases)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
