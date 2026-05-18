-- Аналитические представления. Источник правды для будущего chat layer.

-- Текущие активные предложения с условиями (плоско)
CREATE OR REPLACE VIEW v_offer_current AS
SELECT b.bank_id, b.slug AS bank_slug, b.name AS bank_name, b.is_sber,
       o.offer_id, o.category, o.title, o.url,
       t.terms_id, t.rate_pct, t.rate_kind, t.currency,
       t.amount_min, t.amount_max, t.term_months_min, t.term_months_max,
       t.fee_open, t.fee_service,
       t.early_withdraw, t.capitalization, t.replenishable,
       t.conditions, t.valid_from
  FROM product_offer o
  JOIN bank b USING (bank_id)
  JOIN product_terms t ON t.offer_id = o.offer_id AND t.valid_to IS NULL
 WHERE o.is_active;

-- Топ по ставке в категории (для «самых выгодных»)
CREATE OR REPLACE VIEW v_offer_top_by_rate AS
SELECT category, bank_slug, bank_name, is_sber, offer_id, title, rate_pct,
       amount_min, amount_max, term_months_min, term_months_max,
       RANK() OVER (PARTITION BY category ORDER BY rate_pct DESC NULLS LAST) AS rk
  FROM v_offer_current
 WHERE rate_pct IS NOT NULL;

-- Сбер vs рынок: для каждой категории - ставка Сбера и медианa/макс рынка
CREATE OR REPLACE VIEW v_sber_vs_market AS
WITH market AS (
  SELECT category,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY rate_pct) AS market_median,
         MAX(rate_pct) AS market_max,
         MIN(rate_pct) AS market_min
    FROM v_offer_current
   WHERE NOT is_sber AND rate_pct IS NOT NULL
   GROUP BY category
),
sber AS (
  SELECT category, MAX(rate_pct) AS sber_max, MIN(rate_pct) AS sber_min
    FROM v_offer_current
   WHERE is_sber AND rate_pct IS NOT NULL
   GROUP BY category
)
SELECT m.category,
       s.sber_max, s.sber_min,
       m.market_median, m.market_max, m.market_min,
       (s.sber_max - m.market_median) AS sber_vs_median_pp
  FROM market m FULL JOIN sber s USING (category);

-- Аггрегаты отзывов по банку и теме
CREATE OR REPLACE VIEW v_review_topics AS
SELECT b.slug AS bank_slug, b.name AS bank_name,
       rt.topic, COUNT(*) AS n,
       AVG(r.rating)::NUMERIC(4,2) AS avg_rating
  FROM review r
  JOIN bank b USING (bank_id)
  JOIN review_topic rt USING (review_id)
 GROUP BY b.slug, b.name, rt.topic;

-- Доля негативных отзывов по банку
CREATE OR REPLACE VIEW v_review_sentiment_share AS
SELECT b.slug AS bank_slug, b.name AS bank_name,
       COUNT(*) FILTER (WHERE rs.label='neg')::NUMERIC / NULLIF(COUNT(*),0) AS neg_share,
       COUNT(*) AS total
  FROM review r
  JOIN bank b USING (bank_id)
  LEFT JOIN review_sentiment rs USING (review_id)
 GROUP BY b.slug, b.name;

-- История изменений по предложению (для аудита)
CREATE OR REPLACE VIEW v_offer_history AS
SELECT o.offer_id, b.slug AS bank_slug, o.category, o.title,
       ch.changed_at, ch.diff
  FROM change_history ch
  JOIN product_offer o USING (offer_id)
  JOIN bank b USING (bank_id)
 ORDER BY ch.changed_at DESC;
