-- 036: методология сравнения — диапазон ставки, ПСК, сегмент и подсегмент.
-- Аудит 11.08.2026: в rate_pct кладётся ОДНА граница вилки (у 167 из 187 ипотек
-- и 159 из 186 кредитов вилка есть), а ПСК — единственная величина, которую банк
-- обязан раскрывать по 353-ФЗ — собиралась и выбрасывалась в raw. Сегменты не
-- различались вовсе: премиальная карта за 47 880 руб./год ранжировалась вместе
-- с детской за 0, а беззалоговый кредит — с кредитом под залог недвижимости.
-- NB: одиночного знака процента в файле быть не должно (exec_driver_sql).

ALTER TABLE product_terms ADD COLUMN IF NOT EXISTS rate_min NUMERIC(8, 4);
ALTER TABLE product_terms ADD COLUMN IF NOT EXISTS rate_max NUMERIC(8, 4);
ALTER TABLE product_terms ADD COLUMN IF NOT EXISTS psk_min  NUMERIC(8, 4);
ALTER TABLE product_terms ADD COLUMN IF NOT EXISTS psk_max  NUMERIC(8, 4);

ALTER TABLE product_offer ADD COLUMN IF NOT EXISTS segment     TEXT;
ALTER TABLE product_offer ADD COLUMN IF NOT EXISTS sub_segment TEXT;

-- Бэкфилл диапазонов и ПСК из уже собранного raw (оба источника кладут туда
-- свои имена полей, поэтому берём и те, и другие).
UPDATE product_terms t SET
    rate_min = coalesce(t.rate_min, nullif(t.raw->>'rate_min', '')::numeric,
                        nullif(t.raw->>'rate_from', '')::numeric),
    rate_max = coalesce(t.rate_max, nullif(t.raw->>'rate_max', '')::numeric,
                        nullif(t.raw->>'rate_to', '')::numeric),
    psk_min  = coalesce(t.psk_min,  nullif(t.raw->>'psk_min', '')::numeric,
                        nullif(t.raw->>'rate_psk_from', '')::numeric),
    psk_max  = coalesce(t.psk_max,  nullif(t.raw->>'psk_max', '')::numeric,
                        nullif(t.raw->>'rate_psk_to', '')::numeric)
 WHERE t.valid_to IS NULL AND t.raw IS NOT NULL;

CREATE INDEX IF NOT EXISTS product_offer_segment_idx ON product_offer (category, segment);

-- Витрина отдаёт новые поля (дописываем строго В КОНЕЦ: CREATE OR REPLACE VIEW
-- не разрешает менять порядок существующих колонок).
CREATE OR REPLACE VIEW v_market_rub_offer AS
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
            ELSE '13+' END AS term_bucket,
       o.primary_source,
       t.raw,
       t.rate_min, t.rate_max, t.psk_min, t.psk_max,
       o.segment, o.sub_segment
  FROM product_offer o
  JOIN bank b USING (bank_id)
  JOIN product_terms t ON t.offer_id = o.offer_id AND t.valid_to IS NULL
 WHERE o.is_active
   AND coalesce(t.rate_kind, '') NOT IN ('avg_grade', 'org_rating')
   AND coalesce(upper(t.currency), 'RUB') IN ('RUB', 'РУБ');
