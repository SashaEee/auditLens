-- 034: происхождение оффера — правда вместо константы.
-- До этого в product_offer.primary_source всегда писалось 'sravni_aggregator':
-- аудитор видел ссылку на banki.ru и подпись «источник sravni», а доказать
-- происхождение числа было нечем (аудит вкладки «Рынок» 11.08.2026).
-- Разбираем уже накопленное по домену ссылки и по префиксу external_id.
-- NB: миграции идут через exec_driver_sql, который принимает знак процента за
-- начало placeholder. В LIKE-шаблонах его удваиваем, а в комментариях НЕ пишем
-- вовсе — даже в тексте предупреждения о нём (поймано дважды 11.08.2026:
-- миграция падала молча, set -e обрывал деплой до сборки образа).
UPDATE product_offer
   SET primary_source = 'banki_products'
 WHERE external_id LIKE 'bp\_%%';

UPDATE product_offer
   SET primary_source = 'banki_ratings'
 WHERE external_id LIKE 'banki\_rating\_%%';

UPDATE product_offer
   SET primary_source = 'banki_products'
 WHERE primary_source = 'sravni_aggregator'
   AND url LIKE '%%banki.ru%%';

CREATE INDEX IF NOT EXISTS product_offer_source_idx ON product_offer (primary_source);

-- Витрина отдаёт источник и кешбэк: без первого нельзя показать, откуда число,
-- без второго вторичная колонка карт всегда пуста (объявлена в CATEGORIES).
CREATE OR REPLACE VIEW v_market_rub_offer AS
SELECT b.bank_id, b.slug AS bank_slug, b.name AS bank_name, b.is_sber,
       o.offer_id, o.category, o.title, o.url, o.primary_source,
       t.rate_pct, t.rate_kind, t.amount_min, t.amount_max,
       t.term_months_min, t.term_months_max, t.fee_open, t.fee_service,
       t.grace_days, t.cashback_pct,
       t.early_withdraw, t.capitalization, t.replenishable,
       t.conditions, t.valid_from, t.raw,
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
