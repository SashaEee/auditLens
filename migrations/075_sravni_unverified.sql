-- Отзывы sravni из раннего сборщика без идентификатора банка: сборщик брал
-- общую витрину площадки и записывал её отзывы тому банку, чью страницу
-- открывал. Настоящий банк не восстановить — убираем из индекса и разметки.
DELETE FROM review_annotation
 WHERE url IN (SELECT r.source_url FROM review r
                WHERE r.source = 'sravni_reviews' AND r.raw->>'review_object_id' IS NULL);
DELETE FROM review_index
 WHERE url IN (SELECT r.source_url FROM review r
                WHERE r.source = 'sravni_reviews' AND r.raw->>'review_object_id' IS NULL);
