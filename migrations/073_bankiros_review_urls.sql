-- bankiros: у всех отзывов банка был один url (страница банка), индекс вкладки
-- по url схлопывал 567 отзывов в 14 строк. Ссылка получает якорь с id отзыва,
-- схлопнутые строки индекса и их разметка удаляются — индексация и разметка
-- соберут их заново уже по одной на отзыв.
UPDATE review SET source_url = source_url || '#r-' || source_review_id
 WHERE source = 'bankiros_reviews' AND position('#r-' in source_url) = 0;
DELETE FROM review_annotation
 WHERE url IN (SELECT url FROM review_index
                WHERE source = 'bankiros_reviews' AND position('#r-' in url) = 0);
DELETE FROM review_index
 WHERE source = 'bankiros_reviews' AND position('#r-' in url) = 0;

-- дата из будущего — ошибка разбора площадки: без даты отзыв не попадает в динамику
UPDATE review_index SET dt = NULL WHERE dt > now() + interval '1 day';
