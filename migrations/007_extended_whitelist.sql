-- Migration 007: Расширенный whitelist для лучшего покрытия non-bank сущностей.
-- Добавляем брокеров-аналитиков, рейтинговые агентства, деловую прессу, tech-media.

INSERT INTO source_trust(kind, domain, weight, notes) VALUES
    -- ── Рейтинговые агентства (trust 0.90) ───────────────────────────────
    ('analyst', 'raexpert.ru',         0.90, 'Эксперт РА (рейтинг банков)'),
    ('analyst', 'akra-ratings.ru',     0.90, 'АКРА (рейтинговое агентство)'),
    ('analyst', 'nra-ratings.ru',      0.85, 'НРА'),

    -- ── Брокеры-аналитики (trust 0.80) ──────────────────────────────────
    ('analyst', 'bcs-express.ru',      0.80, 'БКС Экспресс'),
    ('analyst', 'bcs.ru',              0.78, 'БКС'),
    ('analyst', 'finam.ru',            0.78, 'Финам'),
    ('analyst', 'open-broker.ru',      0.75, 'Открытие Брокер'),
    ('analyst', 'gazprombank.investments',0.75,'GazpromBank Investments'),
    ('analyst', 'sber.cib.investing',  0.78, 'Sber CIB'),

    -- ── Финансовая инфраструктура (trust 0.85+) ─────────────────────────
    ('regulator', 'akit.ru',           0.85, 'Ассоциация компаний интернет-торговли'),
    ('regulator', 'asros.ru',          0.85, 'АСРОС (Ассоциация банков)'),
    ('regulator', 'arb.ru',            0.85, 'Ассоциация Российских Банков'),
    ('regulator', 'asv.org.ru',        0.95, 'АСВ (Агентство по страхованию вкладов)'),
    ('regulator', 'rusprofile.ru',     0.75, 'RusProfile (выписки ЕГРЮЛ)'),

    -- ── Деловая пресса (расширение) ─────────────────────────────────────
    ('press', 'profile.ru',            0.78, 'Профиль'),
    ('press', 'tass.ru',               0.85, 'ТАСС'),
    ('press', 'iz.ru',                 0.75, 'Известия'),
    ('press', 'rg.ru',                 0.85, 'Российская газета'),
    ('press', 'banki.ru/news',         0.78, 'Banki.ru новости'),
    ('press', 'realtynews.ru',         0.65, 'Realtynews'),
    ('press', 'banktoday.net',         0.70, 'BankToday'),

    -- ── Tech-media и инвест-блогеры (с осторожностью, trust 0.55-0.65) ──
    ('analyst', 'tinkoff.ru/journal',  0.70, 'Тинькофф Журнал'),
    ('analyst', 'journal.tinkoff.ru',  0.70, 'Тинькофф Журнал (alt)'),
    ('analyst', 'smart-lab.ru',        0.55, 'Smart-lab (инвест-блогеры, mixed)'),
    ('analyst', 'investfunds.ru',      0.65, 'InvestFunds'),
    ('analyst', 'mfd.ru',              0.65, 'MFD.ru (форум инвесторов)'),
    ('analyst', 'spark-interfax.ru',   0.80, 'СПАРК-Интерфакс (financial data)'),

    ('press', 'vc.ru',                 0.55, 'vc.ru (умеренно — есть PR)'),
    ('press', 'habr.com',              0.55, 'Habr (tech, mixed)')
ON CONFLICT (kind, domain) DO UPDATE SET weight = EXCLUDED.weight, notes = EXCLUDED.notes;

-- Аудит расширенного whitelist
SELECT kind, count(*) AS n, round(avg(weight),2) AS avg_w, round(min(weight),2) AS min_w, round(max(weight),2) AS max_w
  FROM source_trust GROUP BY kind ORDER BY avg_w DESC;
