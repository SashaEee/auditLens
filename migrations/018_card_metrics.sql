-- 018: сопоставимые метрики карт (аудит 23.07.2026 — «пустые продукты»).
-- У карт нет ставки как класса: у дебетовых сравнима СТОИМОСТЬ ОБСЛУЖИВАНИЯ
-- (руб./год) и кешбэк, у кредитных — ГРЕЙС-ПЕРИОД и ПСК «от». Поля есть в
-- выдаче sravni (maintenancePriceSS+frequencyNew 100%, interestFreePeriodPurchase
-- 94%, sortCashback 47%), но в модель не извлекались.

ALTER TABLE product_terms ADD COLUMN IF NOT EXISTS grace_days INT;
ALTER TABLE product_terms ADD COLUMN IF NOT EXISTS cashback_pct NUMERIC(6,2);

-- CREATE OR REPLACE не умеет вставлять колонки в середину списка → пересоздаём
-- (CASCADE снесёт зависимую v_sber_vs_market, она восстанавливается ниже)
DROP VIEW IF EXISTS v_market_rub_offer CASCADE;

CREATE VIEW v_market_rub_offer AS
SELECT b.bank_id, b.slug AS bank_slug, b.name AS bank_name, b.is_sber,
       o.offer_id, o.category, o.title, o.url,
       t.rate_pct, t.rate_kind, t.amount_min, t.amount_max,
       t.term_months_min, t.term_months_max, t.fee_open, t.fee_service,
       t.grace_days, t.cashback_pct,
       t.early_withdraw, t.capitalization, t.replenishable,
       t.conditions, t.valid_from,
       CASE WHEN coalesce(t.term_months_min, t.term_months_max) IS NULL THEN 'any'
            WHEN coalesce(t.term_months_min, t.term_months_max) <= 3  THEN '0-3'
            WHEN coalesce(t.term_months_min, t.term_months_max) <= 6  THEN '4-6'
            WHEN coalesce(t.term_months_min, t.term_months_max) <= 12 THEN '7-12'
            ELSE '13+' END AS term_bucket
  FROM product_offer o
  JOIN bank b USING (bank_id)
  JOIN product_terms t ON t.offer_id = o.offer_id AND t.valid_to IS NULL
 WHERE o.is_active
   AND coalesce(t.rate_kind, '') NOT IN ('avg_grade', 'org_rating')
   AND coalesce(upper(t.currency), 'RUB') IN ('RUB', 'РУБ');

-- восстановление зависимой вьюхи (снесена CASCADE выше)
CREATE OR REPLACE VIEW v_sber_vs_market AS
WITH market AS (
  SELECT category,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY rate_pct) AS market_median,
         MAX(rate_pct) AS market_max,
         MIN(rate_pct) AS market_min
    FROM v_market_rub_offer
   WHERE NOT is_sber AND rate_pct IS NOT NULL
   GROUP BY category
),
sber AS (
  SELECT category, MAX(rate_pct) AS sber_max, MIN(rate_pct) AS sber_min
    FROM v_market_rub_offer
   WHERE is_sber AND rate_pct IS NOT NULL
   GROUP BY category
)
SELECT m.category,
       s.sber_max, s.sber_min,
       m.market_median, m.market_max, m.market_min,
       (s.sber_max - m.market_median) AS sber_vs_median_pp
  FROM market m FULL JOIN sber s USING (category);
