"""Закреплённая выдача агрегатора: страница банка приоритетнее витрины."""
from bank_audit.normalizer.offers import _ctx_rank


def test_ctx_rank_order():
    bank_page = {"bank": "vtb", "region": "msk"}
    vitrina_msk = {"amount": 500000, "period_months": 36, "region": "msk"}
    vitrina_spb = {"amount": 500000, "period_months": 36, "region": "sankt-peterburg"}
    assert _ctx_rank(bank_page) < _ctx_rank(vitrina_msk) < _ctx_rank(vitrina_spb)
    assert _ctx_rank({}) == _ctx_rank(vitrina_msk)
    assert _ctx_rank(None) == _ctx_rank(vitrina_msk)
