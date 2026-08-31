"""Сбор эталонного набора страниц для замеров гигиены контекста (этап 1).

Скачивает и складывает СЫРОЙ html реальных страниц банков в fixtures/pages/.
Дальше scripts/context_bench.py гоняет по ним парсер и считает метрики — так
любая правка извлечения проверяется офлайн, без похода в сеть и без трат на LLM.

Запуск (внутри прод-контейнера, где есть обход антибота и сеть):
    docker exec auditlens-app python3 /tmp/context_fixtures.py
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/app/src")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Постоянный том: /tmp внутри контейнера умирает при каждом рестарте,
# и измерительный контур терялся ровно тогда, когда нужен для сверки.
OUT = Path(os.getenv("FIXTURES_DIR",
                     "/home/amzenkovskiy-2127124/auditlens-workspace/fixtures/pages"))

# Страницы подобраны так, чтобы покрыть РАЗНЫЕ типы вёрстки, а не один сайт:
# карточка продукта, каталог с фильтрами, тарифы таблицей, регуляторный документ.
# render=True — сайт за антиботом/SPA, только браузером (иначе получим заглушку).
PAGES = [
    ("sber_deposit_6m", "https://www.sberbank.ru/ru/person/contributions/deposits/vklad-na-6-mesyacev", True),
    ("sber_deposits_catalog", "https://www.sberbank.ru/ru/person/contributions/deposits", True),
    ("sber_credit_cash", "https://www.sberbank.ru/ru/person/credits/money", True),
    ("vtb_deposits", "https://www.vtb.ru/personal/vklady-i-scheta/vklady/", True),
    ("alfa_deposits", "https://alfabank.ru/make-money/deposits/", True),
    ("tbank_deposits", "https://www.tbank.ru/deposit/", False),
    ("gazprom_deposits", "https://www.gazprombank.ru/personal/increase/deposits/", False),
    ("banki_deposits_sber", "https://www.banki.ru/products/deposits/sberbank/", False),
    ("banki_credits_sber", "https://www.banki.ru/products/credits/sberbank/", False),
    ("domrf_izhs_sme", "https://domrfbank.ru/sme/izs/", True),
    ("cbr_psk_page", "https://www.cbr.ru/statistics/bank_sector/psk/", False),
    ("mkb_deposits", "https://mkb.ru/personal/deposits", False),
]

# Признаки, что вместо страницы прилетела заглушка антибота: коротко и без
# осмысленного текста. Такие фикстуры бесполезны для замеров — помечаем.
def _looks_like_stub(content: bytes) -> bool:
    if len(content) < 8000:
        return True
    low = content[:4000].lower()
    return b"support id" in low or b"enable javascript" in low


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    from bank_audit.rag import fetcher
    manifest = []
    for name, url, render in PAGES:
        try:
            fr = fetcher.fetch(url, prefer_browser=render, force_refresh=True)
            content = fr.content or b""
            status = getattr(fr, "status", 0)
            # Не вышло без браузера — пробуем с ним (и наоборот).
            if _looks_like_stub(content) or status >= 400:
                fr = fetcher.fetch(url, prefer_browser=not render, force_refresh=True)
                if not _looks_like_stub(fr.content or b""):
                    content, status = fr.content or b"", getattr(fr, "status", 0)
            stub = _looks_like_stub(content)
            path = OUT / f"{name}.html"
            path.write_bytes(content)
            manifest.append({"name": name, "url": url, "status": status,
                             "bytes": len(content), "via": getattr(fr, "via", ""),
                             "stub": stub})
            mark = "⚠ заглушка" if stub else "✓"
            print(f"  {mark:11} {name:24} {len(content):>8} байт  status={status}")
        except Exception as e:
            manifest.append({"name": name, "url": url, "error": str(e)[:120]})
            print(f"  ✗ {name:24} {type(e).__name__}: {str(e)[:70]}")
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for m in manifest if m.get("bytes") and not m.get("stub"))
    print(f"\nпригодных страниц: {ok} из {len(PAGES)} → {OUT}")
    return 0 if ok >= 6 else 1


if __name__ == "__main__":
    sys.exit(main())
