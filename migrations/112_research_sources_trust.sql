-- 112: доверенные домены исследований из списка руководства (бэклог, 11.11.2026).
-- cbr.ru, raexpert.ru и asros.ru в source_trust уже есть (миграции 006 и 007);
-- здесь — недостающие. Поддомены сопоставляются по окончанию (rag/trust.py),
-- поэтому bankslab.hse.ru и guru.nes.ru покрываются записями hse.ru и nes.ru.
-- Существующие строки не трогаем: вес мог быть поправлен вручную.
-- NB: знак процента в комментариях миграций не ставить (exec_driver_sql).
INSERT INTO source_trust(kind, domain, weight, notes) VALUES
    ('analyst', 'hse.ru',         0.85, 'НИУ ВШЭ: исследования, в том числе лаборатория банковской деятельности (bankslab.hse.ru)'),
    ('analyst', 'nes.ru',         0.85, 'Российская экономическая школа, портал GURU (guru.nes.ru)'),
    ('analyst', 'businesstat.ru', 0.65, 'BusinesStat: обзоры рынков (полные отчёты платные)')
ON CONFLICT (kind, domain) DO NOTHING;
