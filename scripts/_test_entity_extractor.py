"""Unit-test entity_extractor: 6 разных вопросов, разные продукты/банки/языки."""
import asyncio, os, sys
from openai import AsyncOpenAI

# Поднимаем env
from dotenv import load_dotenv
load_dotenv()

from bank_audit.research.entity_extractor import extract_entities
from bank_audit.ai.deep_research import _patch_client_reasoning_effort

TESTS = [
    # (question, expected_min_entities, expected_banks_subset, expected_product_keyword)
    ("Сравни ипотеку Сбер/ВТБ/Альфа/Т-банк",
     4, {"sberbank", "vtb", "alfabank", "tinkoff"}, "ипотек"),
    ("Условия по картам для пенсионеров в разных банках Сбер, ВТБ, Тинькофф",
     3, {"sberbank", "vtb", "tinkoff"}, "пенсион"),
    ("Эквайринг для ИП: тарифы Сбер, Тинькофф, Точка, Модульбанк",
     4, {"sberbank", "tinkoff", "tochka", "modulbank"}, "эквайринг"),
    ("Сравни валютные вклады в долларах в Сбер, ВТБ, Альфа",
     3, {"sberbank", "vtb", "alfabank"}, "вклад"),
    ("Семейная ипотека: ставка, ПВ, требования — Сбер, ВТБ, ДомРФ",
     3, {"sberbank", "vtb", "domrf"}, "ипотек"),
    # Edge case: unicode-дефис
    ("Сравни доверенности Сбер/ВТБ/Альфа/Т‑банк",
     4, {"sberbank", "vtb", "alfabank", "tinkoff"}, "доверен"),
]


async def main():
    client = AsyncOpenAI(
        base_url=os.getenv("LLM_BASE_URL"),
        api_key=os.getenv("LLM_API_KEY"),
        timeout=60.0,
    )
    client = _patch_client_reasoning_effort(client)
    passed = 0
    failed = 0
    for i, (q, min_n, expected_banks, expected_kw) in enumerate(TESTS, 1):
        try:
            ents = await extract_entities(client, q)
            actual_banks = {e.bank_slug for e in ents}
            products_ok = all(expected_kw.lower() in e.product.lower() for e in ents) if ents else False
            n_ok = len(ents) >= min_n
            banks_ok = expected_banks.issubset(actual_banks)
            ok = n_ok and banks_ok and products_ok
            mark = "✅" if ok else "❌"
            print(f"{mark} [{i}/{len(TESTS)}] '{q[:55]}'")
            print(f"   entities: {len(ents)} (>={min_n}), banks: {actual_banks}")
            print(f"   product example: {ents[0].product if ents else 'EMPTY'}")
            print(f"   synonyms: {ents[0].product_synonyms[:4] if ents else []}")
            if ok: passed += 1
            else:
                failed += 1
                print(f"   ⚠ MISMATCH: n_ok={n_ok}, banks_ok={banks_ok} (missing {expected_banks - actual_banks}), products_ok={products_ok}")
        except Exception as e:
            failed += 1
            print(f"❌ [{i}] {q[:55]} → exception: {e}")
    print(f"\n=== {passed}/{len(TESTS)} passed, {failed} failed ===")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
