"""Unit-test triple_extractor: для 3 entities → найти sources → extract triples."""
import asyncio, os
from openai import AsyncOpenAI
from dotenv import load_dotenv
load_dotenv()

from bank_audit.research.entity_extractor import Entity
from bank_audit.research.source_finder import find_gold_sources
from bank_audit.research.triple_extractor import extract_triples
from bank_audit.ai.deep_research import _patch_client_reasoning_effort

ENTITIES = [
    Entity(bank_slug="sberbank", bank_name="Сбербанк", bank_domain="sberbank.ru",
           product="ипотека", product_synonyms=["ипотека","ипотечный кредит"]),
    Entity(bank_slug="vtb", bank_name="ВТБ", bank_domain="vtb.ru",
           product="дебетовая карта", product_synonyms=["дебетовая карта"]),
    Entity(bank_slug="tinkoff", bank_name="Тинькофф", bank_domain="tbank.ru",
           product="эквайринг для бизнеса",
           product_synonyms=["эквайринг","acquiring"]),
]


async def main():
    client = AsyncOpenAI(
        base_url=os.getenv("LLM_BASE_URL"),
        api_key=os.getenv("LLM_API_KEY"),
        timeout=120.0,
    )
    client = _patch_client_reasoning_effort(client)
    print(f"=== triple_extractor test ===\n")
    passed = 0
    for i, e in enumerate(ENTITIES, 1):
        print(f"[{i}/{len(ENTITIES)}] {e.bank_slug} × {e.product}")
        srcs = await find_gold_sources(client, e, top_n=3)
        if not srcs:
            print(f"  ⚠ no sources, skipping\n")
            continue
        triples = await extract_triples(client, e, srcs)
        if triples:
            passed += 1
            print(f"  ✅ {len(triples)} triples:")
            for t in triples[:8]:
                val_str = f"{t.value} {t.unit}".strip()
                print(f"    • {t.attribute}: {val_str} [{t.confidence}] (src #{t.source_idx})")
            if len(triples) > 8: print(f"    ... + {len(triples)-8} more")
        else:
            print(f"  ❌ 0 triples")
        print()
    print(f"=== {passed}/{len(ENTITIES)} entities дали ≥1 triple ===")


if __name__ == "__main__":
    asyncio.run(main())
