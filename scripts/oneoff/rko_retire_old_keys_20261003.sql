-- Шаг 2 чистки РКО (см. rko_cleanup_20261003.sql): после принудительного сбора
-- sravni_rko с новым ключом гасит старые карточки rko_…. Защита от пустого
-- прогона: ничего не делает, если свежих карточек rko2_ меньше 250.
\echo == до
SELECT left(external_id, 5) AS key, is_active, count(*) FROM product_offer
 WHERE category = 'rko' GROUP BY 1, 2 ORDER BY 1, 2;
UPDATE product_offer o SET is_active = false
 WHERE o.category = 'rko' AND left(o.external_id, 4) = 'rko_' AND o.is_active
   AND (SELECT count(*) FROM product_offer n
         WHERE n.category = 'rko' AND left(n.external_id, 5) = 'rko2_'
           AND n.last_seen > now() - interval '6 hours') >= 250;
\echo == после
SELECT left(external_id, 5) AS key, is_active, count(*) FROM product_offer
 WHERE category = 'rko' GROUP BY 1, 2 ORDER BY 1, 2;
