"""Quality checks. Каждый чек — пара (SQL, severity, code).
   Результаты пишутся в quality_flag и в JSON-отчёт workspace/reports/quality_<ts>.json"""
from __future__ import annotations
import json
import os
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
               -- записи реестра ЦБ — справочник, а не предложения: 565 флагов
               -- каждую ночь заглушали настоящие (аудит 03.10, ДАН-14)
               AND o.category <> 'bank_rating'
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
             WHERE t.valid_to IS NULL AND t.rate_pct IS NULL AND o.is_active
               -- у РКО ставки нет по природе: сравнение идёт по плате за пакет;
               -- у брокеров и НПФ rate_pct — оценка организации, часто пустая
               AND o.category NOT IN ('card_credit', 'card_debit', 'other', 'rko', 'bank_rating',
                                      'npf', 'invest_broker')
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
        # Один банк двумя строками (например «ОТП Банк» с каноническим slug и
        # «ОТП БАНК» со slug unknown_…) расщепляет его предложения и завышает
        # «банков в категории» на витрине «Рынок»: ранг Сбера считается по
        # знаменателю, в котором один игрок посчитан дважды.
        "code": "DUP_BANK_BY_NAME",
        "severity": "warn",
        "sql": """
            -- Считаем только ЖИВЫЕ строки: после слияния часть пустых остаётся
            -- намеренно (на них ссылается история, и удалять её дороже, чем
            -- держать пустую строку справочника). Такие дубли витрине не мешают
            -- и поднимать по ним тревогу каждый день незачем.
            WITH live AS (
                SELECT b.bank_id, b.slug, b.name,
                       lower(regexp_replace(b.name, '[^[:alnum:]]', '', 'g')) AS k
                  FROM bank b
                 WHERE EXISTS (SELECT 1 FROM product_offer o
                                WHERE o.bank_id = b.bank_id AND o.is_active)
                    OR EXISTS (SELECT 1 FROM review r WHERE r.bank_id = b.bank_id)
            )
            SELECT 'bank' as et, MIN(bank_id) as eid,
                   jsonb_build_object('count', COUNT(*),
                                      'names', array_agg(DISTINCT name),
                                      'slugs', array_agg(DISTINCT slug)) as detail
              FROM live
             GROUP BY k
            HAVING COUNT(*) > 1
        """,
    },
    {
        # Сегмент продукта витрина определяет по названию («Премиальная»,
        # «Детская»), и на непрозрачных именах правило молчит. LLM читает сам
        # тариф — расхождение показывает, какие названия правило не берёт.
        # Это не ошибка данных, а материал для методологии: чинить регулярку.
        "code": "SEGMENT_MISMATCH",
        "severity": "info",
        "sql": """
            SELECT 'offer' as et, o.offer_id as eid,
                   jsonb_build_object('title', o.title,
                                      'by_rule', COALESCE(o.segment, 'mass'),
                                      'by_text', e.payload->>'client_segment') as detail
              FROM product_offer o
              JOIN offer_enrichment e ON e.offer_id = o.offer_id
             WHERE o.is_active
               AND e.payload->>'client_segment' NOT IN ('unknown', '')
               AND e.payload->>'client_segment' IS DISTINCT FROM COALESCE(o.segment, 'mass')
               -- у модели нет отдельного «детского» сегмента, детские карты она
               -- относит к молодёжным: это разница таксономий, а не дефект
               AND NOT (o.segment = 'kids' AND e.payload->>'client_segment' = 'youth')
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

# ── выбросы ставок: условная или устаревшая ставка, поданная как общая ────────
# Сторож правдоподобия (normalizer/offers.implausible) сравнивает ставку только с
# ключевой: «вклад выше ключевой на 5 п. п.». 03.11.2026 аудитор нашёл на витрине
# «Выгодный старт +» Сбера под 19% при максимуме по факту 12,5% — ставка
# агрегатора для отдельных клиентов. Так же выглядели ещё пять вкладов (пенсионный
# «Юбилейный» 22%, «Зарплатный» 20%…): до ключевой+5 они не дотягивали, но
# отрывались от своей же линейки банка на 4–9 п. п. В августе тот же класс
# (ПСБ «Народный» 30%) закрыли абсолютным порогом — и класс остался.
# Отметка пишется в quality_flag, поэтому витрина, позиция на рынке и
# инструменты ИИ-помощника отсеивают такую ставку одинаково.
OUTLIER_OWN_PP = float(os.getenv("RATE_OUTLIER_OWN_PP", "4"))        # выше медианы своей линейки банка
OUTLIER_MARKET_PP = float(os.getenv("RATE_OUTLIER_MARKET_PP", "2"))  # выше 95-го перцентиля рынка
OUTLIER_CATS = ("deposit", "savings_account")


def _pp(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def outlier_reason(rate: float, bank_med: float | None, bank_n: int, p95: float | None) -> str | None:
    """Причина сомнения или None. Своя линейка — только если в ней от трёх продуктов."""
    over_own = bank_med is not None and bank_n >= 3 and rate > bank_med + OUTLIER_OWN_PP
    over_mkt = p95 is not None and rate > p95 + OUTLIER_MARKET_PP
    if not (over_own or over_mkt):
        return None
    why = (f"ставка {_pp(rate)}% на {_pp(rate - bank_med)} п. п. выше остальных продуктов банка "
           f"(медиана {_pp(bank_med)}%)" if over_own
           else f"ставка {_pp(rate)}% выше 95% предложений рынка ({_pp(p95)}%)")
    return why + " — похоже на условную (для отдельных клиентов) или устаревшую; сверьте с сайтом банка"


def flag_rate_outliers() -> int:
    """Пересчитать отметки выбросов: старые снять, текущие поставить."""
    with db.session() as s:
        rows = s.execute(text("""
            WITH d AS (
              SELECT m.offer_id, m.bank_name, m.category, m.rate_pct::float AS rate
                FROM v_market_rub_offer m
               WHERE m.category = ANY(:cats) AND m.rate_pct > 0.01),
            b AS (SELECT category, bank_name, count(*) AS n,
                         percentile_cont(0.5) WITHIN GROUP (ORDER BY rate) AS med
                    FROM d GROUP BY 1, 2),
            mk AS (SELECT category, percentile_cont(0.95) WITHIN GROUP (ORDER BY rate) AS p95
                     FROM d GROUP BY 1)
            SELECT d.offer_id, d.rate, b.med, b.n, mk.p95
              FROM d JOIN b USING (category, bank_name) JOIN mk USING (category)"""),
            {"cats": list(OUTLIER_CATS)}).all()
        s.execute(text("DELETE FROM quality_flag WHERE entity_type = 'offer' AND code = 'RATE_OUTLIER'"))
        n = 0
        for oid, rate, med, bn, p95 in rows:
            why = outlier_reason(float(rate), float(med) if med is not None else None, int(bn),
                                 float(p95) if p95 is not None else None)
            if why:
                s.execute(text("""INSERT INTO quality_flag(entity_type, entity_id, severity, code, detail)
                                  VALUES ('offer', :e, 'warn', 'RATE_OUTLIER', CAST(:d AS jsonb))"""),
                          {"e": oid, "d": json.dumps({"reason": why}, ensure_ascii=False)})
                n += 1
    return n


def run_quality() -> dict:
    summary: dict[str, int] = {}
    try:
        summary["RATE_OUTLIER"] = flag_rate_outliers()
    except Exception:  # noqa: BLE001 — остальные проверки не должны пострадать
        import logging
        logging.getLogger(__name__).warning("flag_rate_outliers failed", exc_info=True)
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
