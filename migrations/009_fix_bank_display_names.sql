-- Идемпотентный фикс отображаемых имён топ-банков.
-- В bank.name был исторический мусор от ingest'а CBR (для slug=sberbank
-- стояло «РУССКИЙ БАНК СБЕРЕЖЕНИЙ» — мелкий банк с тем же slug'ом).
UPDATE bank SET name='Сбербанк'           WHERE slug='sberbank'   AND name <> 'Сбербанк';
UPDATE bank SET name='ВТБ'                WHERE slug='vtb'        AND name <> 'ВТБ';
UPDATE bank SET name='Альфа-Банк'         WHERE slug='alfabank'   AND name <> 'Альфа-Банк';
UPDATE bank SET name='Тинькофф (Т-Банк)'  WHERE slug='tinkoff'    AND name <> 'Тинькофф (Т-Банк)';
UPDATE bank SET name='Совкомбанк'         WHERE slug='sovcombank' AND name <> 'Совкомбанк';
UPDATE bank SET name='Газпромбанк'        WHERE slug='gazprombank' AND name <> 'Газпромбанк';
UPDATE bank SET name='Россельхозбанк'     WHERE slug='rshb'       AND name <> 'Россельхозбанк';
UPDATE bank SET name='Банк ДОМ.РФ'        WHERE slug='domrf'      AND name <> 'Банк ДОМ.РФ';
UPDATE bank SET name='Открытие'           WHERE slug='otkritie'   AND name <> 'Открытие';
UPDATE bank SET name='Райффайзенбанк'     WHERE slug='raiffeisen' AND name <> 'Райффайзенбанк';
UPDATE bank SET name='Почта Банк'         WHERE slug='pochtabank' AND name <> 'Почта Банк';
UPDATE bank SET name='МКБ'                WHERE slug='mkb'        AND name <> 'МКБ';
UPDATE bank SET name='ПСБ'                WHERE slug='psb'        AND name <> 'ПСБ';
UPDATE bank SET name='Росбанк'            WHERE slug='rosbank'    AND name <> 'Росбанк';
UPDATE bank SET name='Уралсиб'            WHERE slug='uralsib'    AND name <> 'Уралсиб';
UPDATE bank SET name='Ак Барс'            WHERE slug='akbars'     AND name <> 'Ак Барс';
UPDATE bank SET name='МТС Банк'           WHERE slug='mtsbank'    AND name <> 'МТС Банк';
UPDATE bank SET name='Озон Банк'          WHERE slug='ozonbank'   AND name <> 'Озон Банк';
UPDATE bank SET name='Яндекс Банк'        WHERE slug='yandexbank' AND name <> 'Яндекс Банк';
UPDATE bank SET name='Точка Банк'         WHERE slug='tochka'     AND name <> 'Точка Банк';
