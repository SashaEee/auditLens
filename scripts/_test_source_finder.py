"""Unit-test source_finder: для 4 разных entities ищем gold sources."""
import asyncio, os
from openai import AsyncOpenAI
from dotenv import load_dotenv
load_dotenv()

from bank_audit.research.entity_extractor import Entity
from bank_audit.research.source_finder import find_gold_sources
from bank_audit.ai.deep_research import _patch_client_reasoning_effort

ENTITIES = [
    Entity(bank_slug="sberbank", bank_name="Сбербанк", bank_domain="sberbank.ru",
           product="ипотека", product_synonyms=["ипотека","ипотечный кредит","mortgage"]),
    Entity(bank_slug="vtb", bank_name="ВТБ", bank_domain="vtb.ru",
           product="дебетовая карта", product_synonyms=["дебетовая карта","карта","debit card"]),
    Entity(bank_slug="tinkoff", bank_name="Тинькофф", bank_domain="tbank.ru",
           product="доверенность на распоряжение счётом",
           product_synonyms=["доверенность","power of attorney"]),
    Entity(bank_slug="alfabank", bank_name="Альфа-Банк", bank_domain="alfabank.ru",
           product="эквайринг для ИП",
           product_synonyms=["эквайринг","acquiring"]),
]


async def main():
    client = AsyncOpenAI(
        base_url=os.getenv("LLM_BASE_URL"),
        api_key=os.getenv("LLM_API_KEY"),
        timeout=60.0,
    )
    client = _patch_client_reasoning_effort(client)
    print(f"=== source_finder test: {len(ENTITIES)} entities ===\n")
    passed = 0
    for i, e in enumerate(ENTITIES, 1):
        print(f"[{i}/{len(ENTITIES)}] {e.bank_slug} × {e.product[:40]}")
        try:
            srcs = await find_gold_sources(client, e, top_n=3)
            for s in srcs:
                print(f"  • {s.url[:75]} (gold={s.gold_score:.2f}, "
                      f"len={s.length}, trust={s.trust_score:.2f}, "
                      f"prod_url={s.is_product_url}, promo={s.has_promo_url})")
            if srcs:
                passed += 1
                print(f"  ✅ {len(srcs)} sources\n")
            else:
                print(f"  ⚠ 0 sources (БД пустая для этой entity)\n")
        except Exception as exc:
            print(f"  ❌ {exc}\n")
    print(f"\n=== {passed}/{len(ENTITIES)} entities имеют ≥1 source ===")


if __name__ == "__main__":
    asyncio.run(main())
