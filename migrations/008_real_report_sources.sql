-- Migration 008: Источники из реального аналитического отчёта аудитора.
-- Добавляем доменные имена, которые показали себя как полезные в работе.

INSERT INTO source_trust(kind, domain, weight, notes) VALUES
    -- ── IR-страницы публичных компаний / классифайдов ────────────────────
    ('bank_official', 'ir.ciangroup.ru',         0.95, 'CIAN IR (отчётность инвесторам)'),
    ('bank_official', 'ciangroup.ru',            0.95, 'CIAN Group'),
    ('bank_official', 'cian.ru',                 0.90, 'CIAN.ru — данные о сервисе'),
    ('bank_official', 'avito.ru',                0.85, 'Avito (для блога/research)'),
    ('bank_official', 'tech.avito.ru',           0.85, 'Avito Tech блог'),
    ('bank_official', 'about.avito.ru',          0.85, 'Avito (about/корп информация)'),
    ('bank_official', 'domclick.ru',             0.85, 'Домклик (Сбер) — данные о сервисе'),
    ('bank_official', 'дом.рф',                  0.95, 'ДОМ.РФ'),
    ('bank_official', 'xn--h1alffa9f.xn--p1ai',  0.95, 'ДОМ.РФ (punycode)'),

    -- ── Аналитические платформы по трафику и SEO ────────────────────────
    ('analyst', 'similarweb.com',          0.85, 'SimilarWeb (трафик / MAU метрики)'),
    ('analyst', 'liveinternet.ru',         0.75, 'LiveInternet (RU traffic stats)'),
    ('analyst', 'ru.semrush.com',          0.78, 'Semrush'),
    ('analyst', 'mediascope.net',          0.85, 'Mediascope (теле-, digital-аудитория)'),
    ('analyst', 'wciom.ru',                0.85, 'ВЦИОМ опросы'),

    -- ── Презентации и инвест-материалы ──────────────────────────────────
    ('analyst', 'ppt-online.org',          0.65, 'Хостинг презентаций (часто IR-материалы)'),
    ('analyst', 'investproekt.ru',         0.65, 'InvestProekt'),

    -- ── Финансовая аналитика ────────────────────────────────────────────
    ('analyst', 'frankrg.com',             0.85, 'Frank RG (рейтинги/исследования)'),
    ('analyst', 'frankmedia.ru',           0.78, 'Frank Media'),
    ('analyst', 'ratingbanki.ru',          0.65, 'Рейтинг банков'),
    ('analyst', 'bankodrom.ru',            0.55, 'Банкодром'),
    ('analyst', 'banki-vsem.com',          0.50, 'Banki-vsem'),

    -- ── Региональная пресса (для cross-проверки) ────────────────────────
    ('press', 'ngs24.ru',                  0.65, 'НГС Красноярск'),
    ('press', 'ngs.ru',                    0.65, 'НГС Новосибирск'),
    ('press', 'potokmedia.ru',             0.55, 'PotokMedia'),
    ('press', 'realty.rbc.ru',             0.85, 'РБК Недвижимость'),
    ('press', 'banki.realty.ru',           0.65, 'Banki Realty'),

    -- ── Маркетинговая отрасль (для отзывов от риелторов/агентств) ──────
    ('analyst', 'yagla.ru',                0.55, 'Yagla (маркетинговая отрасль)'),
    ('analyst', 'callibri.ru',             0.55, 'Callibri (data на лидогенерацию)'),

    -- ── Дополнительные деловые СМИ ──────────────────────────────────────
    ('press', 'thebell.io',                0.78, 'The Bell'),
    ('press', 'cnews.ru',                  0.75, 'CNews (tech/banking)'),
    ('press', 'finanz.ru',                 0.70, 'Finanz.ru'),
    ('press', 'bfm.ru',                    0.75, 'Business FM'),
    ('press', 'plusworld.ru',              0.75, 'PLUSworld (банки/платежи)'),
    ('press', 'finversia.ru',              0.70, 'Finversia'),
    ('press', 'banking.ru',                0.65, 'Banking.ru'),

    -- ── Госорганы и официальные агрегаторы данных ───────────────────────
    ('regulator', 'gov.ru',                0.95, 'Правительство РФ'),
    ('regulator', 'минфин.рф',             1.00, 'Минфин (рус)'),
    ('regulator', 'минэкономразвития.рф',  0.95, 'Минэкономразвития'),
    ('regulator', 'data.gov.ru',           0.95, 'Открытые данные'),

    -- ── Investor portals ───────────────────────────────────────────────
    ('analyst', 'tradingview.com',         0.70, 'TradingView (RU секция)'),
    ('analyst', 'ru.tradingview.com',      0.70, 'TradingView RU'),
    ('analyst', 'invest.cbr.ru',           1.00, 'ЦБ инвест-данные'),
    ('analyst', 'investfunds.ru',          0.65, 'InvestFunds')
ON CONFLICT (kind, domain) DO UPDATE SET weight = EXCLUDED.weight, notes = EXCLUDED.notes;

-- Reload counts
SELECT kind, count(*) AS n, round(avg(weight),2) AS avg_w
  FROM source_trust GROUP BY kind ORDER BY avg_w DESC;
