"""Unit-test schema_normalizer: разнородные атрибуты от 3 банков → каноны."""
import asyncio, os
from openai import AsyncOpenAI
from dotenv import load_dotenv
load_dotenv()

from bank_audit.research.triple_extractor import Triple
from bank_audit.research.schema_normalizer import normalize_schema, apply_normalization
from bank_audit.ai.deep_research import _patch_client_reasoning_effort

# Симулируем триплы от 3 банков, где одни и те же поля названы по-разному
TRIPLES = [
    # Сбер
    Triple("sberbank", "годовая_комиссия",       "0",   "₽",  source_url="s.ru/1", excerpt="..."),
    Triple("sberbank", "минимальная_ставка",     "6.0", "%",  source_url="s.ru/1", excerpt="..."),
    Triple("sberbank", "лимит_снятия_наличных",  "30000","₽/день", source_url="s.ru/1", excerpt="..."),
    # ВТБ
    Triple("vtb", "плата_за_обслуживание",       "0",   "₽",  source_url="v.ru/1", excerpt="..."),
    Triple("vtb", "ставка_от",                    "5.5", "%",  source_url="v.ru/1", excerpt="..."),
    Triple("vtb", "лимит_наличных_в_сутки",       "350000","₽", source_url="v.ru/1", excerpt="..."),
    # Альфа
    Triple("alfabank", "комиссия_годовая_тариф", "0",   "₽",  source_url="a.ru/1", excerpt="..."),
    Triple("alfabank", "начальная_ставка",       "5.9", "%",  source_url="a.ru/1", excerpt="..."),
    Triple("alfabank", "снятие_наличных_лимит",  "150000","₽", source_url="a.ru/1", excerpt="..."),
    # Уникальный
    Triple("alfabank", "право_передоверия",       "да",  "",   source_url="a.ru/2", excerpt="..."),
]


async def main():
    client = AsyncOpenAI(
        base_url=os.getenv("LLM_BASE_URL"),
        api_key=os.getenv("LLM_API_KEY"),
        timeout=60.0,
    )
    client = _patch_client_reasoning_effort(client)
    mapping = await normalize_schema(client, TRIPLES)
    print("=== Mapping ===")
    for orig, canon in mapping.items():
        marker = "→" if orig != canon else "≡"
        print(f"  {orig:35s} {marker} {canon}")
    print()
    # Применяем и смотрим на канонические группы
    normalized = apply_normalization(TRIPLES, mapping)
    groups: dict[str, list] = {}
    for t in normalized:
        groups.setdefault(t.attribute, []).append((t.entity_bank_slug, t.value, t.unit))
    print("=== Канонические группы ===")
    for canon, vals in sorted(groups.items()):
        print(f"  {canon}:")
        for bank, val, unit in vals:
            print(f"    {bank}: {val} {unit}")
    # Проверка корректности
    expected_groups = 4   # ~ годовая_комиссия, ставка, лимит_снятия, право_передоверия
    actual_groups = len(groups)
    ok = actual_groups <= expected_groups + 1
    print(f"\nГрупп получилось: {actual_groups} (ожидаемо ~{expected_groups})")
    print(f"{'✅ OK' if ok else '❌ слишком много несгруппированных'}")


if __name__ == "__main__":
    asyncio.run(main())
