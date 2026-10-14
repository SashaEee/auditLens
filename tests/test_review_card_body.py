"""Отзывы раннего HTML-сборщика banki.ru: в тексте — карточка ленты с CSS (обращение 09.10)."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.rag import bankiru_fts as F  # noqa: E402

CARD = ("Проблема решена\nДобрый день. У моего 14 летнего сына истек срок пользования детской картой. "
        "При замене карты ему стали навязывать доп услуги. После...\n09.08.2026 09:26\n1\n"
        ".l2556a5ca{align-items:center;border-radius:4px;display:inline-flex;font-weight:400}"
        ".la30813d5{background-color:#ef2d1d;color:#fff}.l5ae9395d{background-color:#039be5;color:#fff}")


def test_card_chrome_and_css_removed():
    out = F.clean_body(CARD)
    assert out.startswith("Добрый день.") and out.endswith("После...")
    assert "{" not in out and "09.08.2026" not in out and "Проблема решена" not in out


def test_plain_text_untouched():
    t = "Проблема решена\nБанк вернул деньги 09.08.2026 09:26\n1"
    assert F.clean_body(t) == t                      # без CSS — ничего не трогаем
    assert F.clean_body("  Списали {комиссию} дважды  ") == "Списали {комиссию} дважды"
    assert F.clean_body(None) == ""


def test_banki_rid():
    assert F.banki_rid("https://www.banki.ru/services/responses/bank/response/13287743/") == "13287743"
    assert F.banki_rid("https://www.banki.ru/services/responses/bank/response/13287743") == "13287743"
    assert F.banki_rid("https://www.sravni.ru/x") is None


def test_slice_products_are_one_condition_everywhere():
    """Срезы в списке продуктов (обращения 08–14.10): названия одни в коде и в
    SQL-функции, и все запросы вкладки фильтруют продукт через неё по срезам из
    LLM-разметки — иначе срез разъедется между вкладками."""
    import re
    from pathlib import Path

    from bank_audit.rag import reviews_dash as D
    root = Path(__file__).resolve().parents[1]
    sql = (root / "migrations" / "099_review_slices.sql").read_text(encoding="utf-8")
    for label in D.PSEUDO_PRODUCTS:
        assert f"'{label}'" in sql, label
    assert "Зарплатные проекты" in D.PSEUDO_PRODUCTS
    for rel in ("rag/reviews_dash.py", "rag/reviews_work.py", "rag/bankiru_fts.py"):
        src = (root / "src" / "bank_audit" / rel).read_text(encoding="utf-8")
        assert not re.search(r"\b[a-z]\.product = :(product|pr)\b", src), rel
        assert ".channels, " not in src.split("al_product_is", 1)[-1][:200] or "slices" in src, rel
        for call in re.findall(r"al_product_is\(([^)]*)\)", src):
            assert ".slices" in call, (rel, call)
