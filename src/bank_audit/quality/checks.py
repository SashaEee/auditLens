"""Quality checks. Каждый чек — пара (SQL, severity, code).
   Результаты пишутся в quality_flag и в JSON-отчёт workspace/reports/quality_<ts>.json"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import text
from .. import db
from ..config import Settings

CHECKS = [
    {
        "code": "STALE_OFFER",
        "severity": "warn",
        "sql": """
            SELECT 'offer' as et, o.offer_id as eid,
                   jsonb_build_object('last_seen', o.last_seen) as detail
              FROM product_offer o
             WHERE o.is_active AND o.last_seen < now() - interval '14 days'
        """,
    },
    {
        "code": "MISSING_RATE",
        "severity": "warn",
        "sql": """
            SELECT 'terms' as et, t.terms_id as eid,
                   jsonb_build_object('offer_id', t.offer_id) as detail
              FROM product_terms t
              JOIN product_offer o USING(offer_id)
             WHERE t.valid_to IS NULL AND t.rate_pct IS NULL
               AND o.category NOT IN ('card_credit', 'card_debit', 'other')
        """,
    },
    {
        "code": "RATE_JUMP",
        "severity": "error",
        "sql": """
            WITH cur AS (
              SELECT offer_id, rate_pct FROM product_terms WHERE valid_to IS NULL
            ),
            prev AS (
              SELECT DISTINCT ON (offer_id) offer_id, rate_pct
                FROM product_terms WHERE valid_to IS NOT NULL
                ORDER BY offer_id, valid_to DESC
            )
            SELECT 'offer' as et, c.offer_id as eid,
                   jsonb_build_object('prev', p.rate_pct, 'cur', c.rate_pct) as detail
              FROM cur c JOIN prev p USING (offer_id)
             WHERE p.rate_pct IS NOT NULL AND c.rate_pct IS NOT NULL
               AND ABS(c.rate_pct - p.rate_pct) / NULLIF(p.rate_pct,0) > 0.25
        """,
    },
    {
        "code": "BANK_COVERAGE_LOW",
        "severity": "warn",
        "sql": """
            -- Категории, где покрытие < 30% от действующих банков (CBR registry)
            SELECT 'category' as et, 0::bigint as eid,
                   jsonb_build_object(
                     'category', category::text,
                     'banks_total', banks_total,
                     'banks_with_offers', banks_with_offers,
                     'coverage_pct', coverage_pct
                   ) as detail
              FROM v_bank_coverage
             WHERE coverage_pct IS NOT NULL
               AND coverage_pct < 30
               AND banks_total > 10
        """,
    },
    {
        "code": "BANK_NO_OFFERS",
        "severity": "info",
        "sql": """
            -- Банки из CBR registry без единого активного оффера ни в одной категории
            SELECT 'bank' as et, b.bank_id as eid,
                   jsonb_build_object('name', b.name, 'license', b.cbr_license_no) as detail
              FROM bank b
             WHERE COALESCE(b.cbr_status,'active') = 'active'
               AND NOT EXISTS (
                 SELECT 1 FROM product_offer o
                  WHERE o.bank_id = b.bank_id AND o.is_active
               )
        """,
    },
    {
        "code": "DUP_REVIEW_BY_TEXT",
        "severity": "info",
        "sql": """
            SELECT 'review' as et, MIN(review_id) as eid,
                   jsonb_build_object('count', COUNT(*), 'bank_id', bank_id) as detail
              FROM review
             GROUP BY bank_id, md5(text)
            HAVING COUNT(*) > 1
        """,
    },
]

def run_quality() -> dict:
    summary: dict[str, int] = {}
    settings = Settings.load()
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    flags = []
    with db.session() as s:
        for c in CHECKS:
            rows = s.execute(text(c["sql"])).all()
            summary[c["code"]] = len(rows)
            for et, eid, detail in rows:
                s.execute(text("""
                    INSERT INTO quality_flag(entity_type, entity_id, severity, code, detail)
                    VALUES (:et,:eid,:sev,:code, CAST(:d AS jsonb))
                """), {"et": et, "eid": eid, "sev": c["severity"], "code": c["code"],
                       "d": json.dumps(detail, ensure_ascii=False, default=str)})
                flags.append({"entity_type": et, "entity_id": eid,
                              "code": c["code"], "severity": c["severity"], "detail": detail})
    out = settings.reports_dir / f"quality_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
    out.write_text(json.dumps({"summary": summary, "flags": flags},
                              ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {"summary": summary, "report": str(out)}
