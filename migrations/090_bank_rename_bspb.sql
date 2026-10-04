-- banki.ru переименовал «Банк «Санкт-Петербург»» в «БСПБ» (последний отзыв под
-- старым именем — 19.08.2026): жалобы банка распадались на две строки, доля,
-- тренд и сравнение с рынком по нему искажались (аудит 03.10, ОТЗ-14). Новые
-- строки склеивает rag/bankiru_fts.BANK_RENAMES; здесь — накопленное.
-- Применять ПОСЛЕ выкладки кода с BANK_RENAMES: синхронизация индекса пишет
-- имя банка заново и вернула бы старое.
UPDATE review_index   SET bank = 'БСПБ' WHERE bank = 'Банк «Санкт-Петербург»';
UPDATE signal_journal SET bank = 'БСПБ' WHERE bank = 'Банк «Санкт-Петербург»';
INSERT INTO review_subscription (username, bank, product, created_at)
     SELECT username, 'БСПБ', product, created_at FROM review_subscription
      WHERE bank = 'Банк «Санкт-Петербург»'
ON CONFLICT DO NOTHING;
DELETE FROM review_subscription WHERE bank = 'Банк «Санкт-Петербург»';
