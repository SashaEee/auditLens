-- 038: перенести накопительные счета по ТИПУ продукта, а не по названию.
-- Миграция 037 переносила по слову «накопительн» в названии, и 42 счёта
-- 28 банков (включая два сберовских и лидера рынка МТС) остались во «Вкладах»:
-- источник называет их «Ozon Счёт», «Альфа-Счёт», «Сейф» — слова в имени нет,
-- а тип продукта в данных есть (depositType='accumulative').
-- Нормализатор уже раскладывает новые сборы правильно; здесь чиним накопленное.
-- NB: одиночного знака процента в файле быть не должно (exec_driver_sql).

UPDATE product_offer o
   SET category = 'savings_account'
  FROM product_terms t
 WHERE t.offer_id = o.offer_id
   AND t.valid_to IS NULL
   AND o.category = 'deposit'
   AND lower(coalesce(t.raw->>'deposit_type', '')) IN ('accumulative', 'saving', 'savings')
   -- защита от коллизии с уникальным ключом (bank_id, category, external_id)
   AND NOT EXISTS (
        SELECT 1 FROM product_offer x
         WHERE x.bank_id = o.bank_id
           AND x.category = 'savings_account'
           AND x.external_id = o.external_id);

-- Обратный случай: срочный вклад со словом «накопительный» в имени уехал в
-- накопительные миграцией 037 — возвращаем по типу.
UPDATE product_offer o
   SET category = 'deposit'
  FROM product_terms t
 WHERE t.offer_id = o.offer_id
   AND t.valid_to IS NULL
   AND o.category = 'savings_account'
   AND lower(coalesce(t.raw->>'deposit_type', '')) IN ('classic', 'grow', 'deal', 'term', 'urgent')
   AND NOT EXISTS (
        SELECT 1 FROM product_offer x
         WHERE x.bank_id = o.bank_id
           AND x.category = 'deposit'
           AND x.external_id = o.external_id);
