"""Минимальные smoke-тесты без сети и БД: парсеры разбирают фикстуры."""
from bank_audit.sources.sravni_aggregator import SravniAggregatorAdapter, SELECTORS

FIXTURE = b"""
<html><body>
  <article data-qa="product-card">
    <h3 data-qa="bank-name">Сбербанк</h3>
    <div data-qa="product-name">Лучший%</div>
    <div data-qa="rate">до 16,5%</div>
    <div data-qa="amount">от 100 000 до 5 000 000 руб</div>
    <div data-qa="term">от 6 до 12 мес</div>
    <a href="/vklady/sber/luchshij/"></a>
  </article>
</body></html>
"""

def test_aggregator_parses_card(tmp_path):
    a = SravniAggregatorAdapter(settings=None, raw_store=None)
    items = list(a.parse_offers(FIXTURE, {"name":"t","category":"deposit","filter_context":{}}))
    assert len(items) == 1
    o = items[0]
    assert o.bank_name_raw == "Сбербанк"
    assert o.rate_pct is not None and float(o.rate_pct) == 16.5
    assert o.amount_min == 100000 and o.amount_max == 5000000
    assert o.term_months_min == 6 and o.term_months_max == 12
