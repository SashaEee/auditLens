-- Переименования банков на площадке: история одного банка распадалась на два
-- названия («Точка» до февраля 2026 → «Точка Банк»). Новые строки индекс
-- склеивает сам (rag/bankiru_fts.BANK_RENAMES), здесь — уже накопленные.
UPDATE review_index SET bank = 'Точка Банк'        WHERE bank = 'Точка';
UPDATE review_index SET bank = 'SBI Банк'          WHERE bank = 'SBI Bank';
UPDATE review_index SET bank = 'А7 Финансы - ПСБ'  WHERE bank = 'А7-финансы ПСБ';
UPDATE review_index SET bank = 'Долинск Банк'      WHERE bank = 'Долинск';
