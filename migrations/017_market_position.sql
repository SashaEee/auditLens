-- 017: честная база сравнения рынка (этап 1 редизайна вкладки «Позиция»).
-- v_market_rub_offer — очищенный слой: только активные рублёвые офферы витринных
-- категорий, без псевдо-офферов рейтингов (avg_grade/org_rating), с бакетом срока.
-- v_sber_vs_market пересаживается на неё же — Обзор и вкладка считают одинаково.
-- ⚠ на проде вьюхи катятся руками (psql), не забыть про analytics/views.sql.

CREATE OR REPLACE VIEW v_market_rub_offer AS
SELECT b.bank_id, b.slug AS bank_slug, b.name AS bank_name, b.is_sber,
       o.offer_id, o.category, o.title, o.url,
       t.rate_pct, t.rate_kind, t.amount_min, t.amount_max,
       t.term_months_min, t.term_months_max, t.fee_open, t.fee_service,
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

-- Сбер vs рынок v2: те же колонки, но чистая база (раньше медиану загрязняли
-- псевдо-офферы рейтингов и валютные строки)
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
