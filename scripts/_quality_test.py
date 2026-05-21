"""Качественный e2e тест на разных банковских темах.

Запускает 5 разных вопросов, замеряет:
  • Время
  • Объём markdown отчёта
  • Кол-во шагов
  • Покрытие fact-extract по банкам
  • Качество claim-verify

Цель — убедиться что pipeline работает на ЛЮБОЙ теме / банке, а не только
на тех что я тестировал руками.
"""
import asyncio, json, time, httpx, sys

URL = "http://127.0.0.1:8000/api/ai/analyze"

# Разные темы, разные банки, разные форматы вопросов — стресс-тест
TESTS = [
    # Классические продукты
    "Сравни валютные вклады в долларах в Сбер, ВТБ, Альфа",
    "Дебетовые карты для пенсионеров — Сбер, Почта Банк, ВТБ",
    # Сложные нишевые
    "Эквайринг для ИП: тарифы Сбер, Тинькофф, Точка, Модульбанк",
    # Бизнес-сегмент
    "РКО для малого бизнеса в Сбер vs Альфа vs Тинькофф",
    # Социальный продукт
    "Семейная ипотека: ставка, ПВ, требования — Сбер, ВТБ, ДомРФ",
]


async def run_one(client: httpx.AsyncClient, question: str, idx: int) -> dict:
    print(f"\n[{idx+1}/{len(TESTS)}] Q: {question[:80]}")
    t0 = time.time()
    txt_total = 0
    events = []
    plan_steps = 0
    banks_in_plan = set()
    bank_facts_lines = {}
    sources_count = 0
    verified = unverified = 0

    try:
        async with client.stream("POST", URL,
                                 json={"question": question, "force_deep": True},
                                 timeout=240.0) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    d = json.loads(line[6:])
                except Exception:
                    continue
                t = d.get("type")
                events.append(t)
                if t == "text":
                    txt_total += len(d.get("chunk", ""))
                elif t == "plan":
                    plan_steps = len(d.get("steps", []))
                    for s in d.get("steps", []):
                        e = (s.get("entity") or "").lower()
                        if e: banks_in_plan.add(e)
                elif t == "sources":
                    sources_count = len(d.get("sources", []))
                elif t == "claim_check":
                    verified = d.get("verified", 0)
                elif t == "verification":
                    unv = d.get("unverified", [])
                    unverified = len(unv) if isinstance(unv, list) else int(unv or 0)
                elif t == "done":
                    break
    except httpx.ReadTimeout:
        print(f"  ⚠ TIMEOUT (>240s)")

    dt = time.time() - t0
    return {
        "q": question[:50],
        "time_s": round(dt, 1),
        "text_chars": txt_total,
        "plan_steps": plan_steps,
        "banks_in_plan": len(banks_in_plan),
        "sources": sources_count,
        "verified": verified,
        "unverified": unverified,
        "events_count": len(events),
        "has_done": "done" in events,
    }


async def main():
    print("=" * 100)
    print(f"AuditLens Quality Test — {len(TESTS)} вопросов")
    print("=" * 100)
    results = []
    async with httpx.AsyncClient(http2=False) as client:
        for i, q in enumerate(TESTS):
            r = await run_one(client, q, i)
            results.append(r)
            verdict = "✅" if (r["text_chars"] > 2000 and r["has_done"] and r["time_s"] < 180) else "⚠"
            print(f"  {verdict} {r['time_s']}s, {r['text_chars']} chars, "
                  f"{r['plan_steps']} шагов, {r['banks_in_plan']} банков, "
                  f"{r['sources']} src, {r['verified']}✓/{r['unverified']}✗")

    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)
    avg_time = sum(r["time_s"] for r in results) / len(results)
    avg_chars = sum(r["text_chars"] for r in results) / len(results)
    success = sum(1 for r in results if r["text_chars"] > 2000 and r["has_done"])
    print(f"  Успешных: {success}/{len(results)}")
    print(f"  Среднее время: {avg_time:.1f}s")
    print(f"  Средний объём: {avg_chars:.0f} chars markdown")
    for r in results:
        print(f"  • {r['q']:55} → {r['time_s']:5.1f}s · {r['text_chars']:5} chars · {r['banks_in_plan']} banks")
    # JSON dump для дальнейшего анализа
    with open("/tmp/quality_test.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nDetails: /tmp/quality_test.json")


if __name__ == "__main__":
    asyncio.run(main())
