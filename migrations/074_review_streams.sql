-- Когда отзыв попал к нам: любая цифра вкладки воспроизводима «как было на
-- момент выпуска». Для уже накопленных строк — время миграции.
ALTER TABLE review_index ADD COLUMN IF NOT EXISTS ingested_at timestamptz DEFAULT now();

-- Свой сбор banki.ru дублировал внешний корпус под другой ссылкой (со слешем
-- на конце): 1 312 отзывов жили в индексе дважды. Оставляем копию корпуса.
DELETE FROM review_annotation
 WHERE url IN (SELECT r.url FROM review_index r
                WHERE r.source = 'banki_reviews'
                  AND EXISTS (SELECT 1 FROM review_index c
                               WHERE c.source = 'bankiru'
                                 AND c.url IN ('https://www.banki.ru/services/responses/bank/response/'
                                               || substring(r.url from 'response/(\d+)'),
                                               'https://www.banki.ru/services/responses/bank/response/'
                                               || substring(r.url from 'response/(\d+)') || '/')));
DELETE FROM review_index r
 WHERE r.source = 'banki_reviews'
   AND EXISTS (SELECT 1 FROM review_index c
                WHERE c.source = 'bankiru'
                  AND c.url IN ('https://www.banki.ru/services/responses/bank/response/'
                                || substring(r.url from 'response/(\d+)'),
                                'https://www.banki.ru/services/responses/bank/response/'
                                || substring(r.url from 'response/(\d+)') || '/'));
