-- Migration 006: Расширенный whitelist верифицированных источников.
-- Принцип: только проверенные домены (регуляторы, IR-страницы публичных компаний,
-- деловая пресса) попадают в RAG для аудит-режима. Всё остальное → trust=0.1.

-- ── Регуляторы и официальные раскрытия (trust 1.0) ───────────────────────
INSERT INTO source_trust(kind, domain, weight, notes) VALUES
    ('regulator', 'cbr.ru',           1.00, 'Центробанк РФ'),
    ('regulator', 'fns.gov.ru',       1.00, 'ФНС РФ'),
    ('regulator', 'minfin.gov.ru',    1.00, 'Минфин РФ'),
    ('regulator', 'moex.com',         1.00, 'Мосбиржа'),
    ('regulator', 'e-disclosure.ru',  1.00, 'Раскрытие корпоративной информации (Интерфакс)'),
    ('regulator', 'rosstat.gov.ru',   1.00, 'Росстат')
ON CONFLICT (kind, domain) DO UPDATE SET weight = EXCLUDED.weight, notes = EXCLUDED.notes;

-- ── IR-страницы публичных компаний (trust 0.95) ─────────────────────────
INSERT INTO source_trust(kind, domain, weight, notes) VALUES
    ('bank_official', 'sberbank.com',   0.95, 'Sberbank Group IR'),
    ('bank_official', 'sberbank.ru',    0.95, 'Сбербанк (ru)'),
    ('bank_official', 'vtb.ru',         0.95, 'ВТБ'),
    ('bank_official', 'vtbcapital.com', 0.95, 'ВТБ Капитал IR'),
    ('bank_official', 'tbank.ru',       0.95, 'Т-Банк (Tinkoff)'),
    ('bank_official', 'alfabank.ru',    0.95, 'Альфа-Банк'),
    ('bank_official', 'sovcombank.ru',  0.95, 'Совкомбанк'),
    ('bank_official', 'gazprombank.ru', 0.95, 'Газпромбанк'),
    ('bank_official', 'rshb.ru',        0.95, 'Россельхозбанк'),
    ('bank_official', 'open.ru',        0.95, 'Банк Открытие'),
    ('bank_official', 'raiffeisen.ru',  0.95, 'Райффайзенбанк'),
    ('bank_official', 'pochtabank.ru',  0.95, 'Почта Банк'),
    ('bank_official', 'mkb.ru',         0.95, 'МКБ'),
    ('bank_official', 'akbars.ru',      0.95, 'Ак Барс'),
    ('bank_official', 'mtsbank.ru',     0.95, 'МТС Банк'),
    ('bank_official', 'finance.ozon.ru',0.95, 'Озон Банк'),
    ('bank_official', 'psbank.ru',      0.95, 'ПСБ'),
    ('bank_official', 'unicreditbank.ru',0.95,'ЮниКредит'),
    ('bank_official', 'uralsib.ru',     0.95, 'Уралсиб'),
    ('bank_official', 'rosbank.ru',     0.95, 'Росбанк'),
    ('bank_official', 'bspb.ru',        0.95, 'БСПБ'),
    ('bank_official', 'domrfbank.ru',   0.95, 'Банк ДОМ.РФ'),
    ('bank_official', 'lockobank.ru',   0.95, 'Локо-Банк'),
    ('bank_official', 'sinarabank.ru',  0.95, 'Синара'),
    ('bank_official', 'rencredit.ru',   0.95, 'Ренессанс Кредит'),
    ('bank_official', 'rsb.ru',         0.95, 'Русский Стандарт'),
    ('bank_official', 'norvikbank.ru',  0.95, 'Норвик Банк'),
    ('bank_official', 'homecredit.ru',  0.95, 'Хоум Кредит')
ON CONFLICT (kind, domain) DO UPDATE SET weight = EXCLUDED.weight, notes = EXCLUDED.notes;

-- ── Деловая пресса (проверенные, trust 0.85) ─────────────────────────────
INSERT INTO source_trust(kind, domain, weight, notes) VALUES
    ('press', 'vedomosti.ru',     0.85, 'Ведомости'),
    ('press', 'rbc.ru',           0.85, 'РБК'),
    ('press', 'companies.rbc.ru', 0.85, 'РБК Компании'),
    ('press', 'interfax.ru',      0.85, 'Интерфакс'),
    ('press', 'kommersant.ru',    0.85, 'Коммерсантъ'),
    ('press', 'forbes.ru',        0.85, 'Forbes Russia'),
    ('press', 'frankrg.com',      0.85, 'Frank RG (рейтинги банков)'),
    ('press', 'expert.ru',        0.80, 'Эксперт'),
    ('press', 'realty.ria.ru',    0.80, 'РИА Недвижимость')
ON CONFLICT (kind, domain) DO UPDATE SET weight = EXCLUDED.weight, notes = EXCLUDED.notes;

-- ── Аналитические дома (trust 0.80) ──────────────────────────────────────
INSERT INTO source_trust(kind, domain, weight, notes) VALUES
    ('analyst', 'alfacapital.ru',  0.80, 'Альфа-Капитал'),
    ('analyst', 'tinkoff.ru',      0.80, 'Т-Банк аналитика'),
    ('analyst', 'fomag.ru',        0.70, 'Financial One (FOMAG)'),
    ('analyst', 'investing.com',   0.70, 'Investing.com Russia'),
    ('analyst', 'rusbonds.ru',     0.70, 'RusBonds'),
    ('analyst', 'lmsic.com',       0.65, 'LMSIC')
ON CONFLICT (kind, domain) DO UPDATE SET weight = EXCLUDED.weight, notes = EXCLUDED.notes;

-- ── Bank aggregators (расширяем существующие) ───────────────────────────
INSERT INTO source_trust(kind, domain, weight, notes) VALUES
    ('aggregator', 'sravni.ru',    0.70, 'Сравни.ру'),
    ('aggregator', 'banki.ru',     0.70, 'Banki.ru'),
    ('aggregator', 'bankiros.ru',  0.65, 'Bankiros'),
    ('aggregator', 'finanso.com',  0.60, 'Finanso')
ON CONFLICT (kind, domain) DO UPDATE SET weight = EXCLUDED.weight, notes = EXCLUDED.notes;

-- ── Отзовики (trust 0.50) ───────────────────────────────────────────────
INSERT INTO source_trust(kind, domain, weight, notes) VALUES
    ('forum', 'irecommend.ru', 0.50, 'iRecommend'),
    ('forum', 'otzovik.com',   0.50, 'Отзовик'),
    ('forum', 'vc.ru',         0.55, 'vc.ru (умеренно — есть PR)')
ON CONFLICT (kind, domain) DO UPDATE SET weight = EXCLUDED.weight, notes = EXCLUDED.notes;

-- Удобный view: все verified домены
CREATE OR REPLACE VIEW v_verified_sources AS
SELECT source_id, kind, domain, weight, notes
  FROM source_trust
 WHERE weight >= 0.5
 ORDER BY weight DESC, kind, domain;
