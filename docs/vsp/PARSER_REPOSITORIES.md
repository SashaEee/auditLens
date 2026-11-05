# Готовые решения для сбора отзывов 2ГИС и Яндекс Карт

Обзор сделан 05.11.2026. Исходники каждого репозитория прочитаны; там, где это было дёшево, ключевые утверждения проверены живыми запросами (около 1 запроса в секунду, без капчи).

**Итог.** Готовой библиотеки, которая решает задачу целиком, нет:
- реестр тысяч отделений;
- продолжение после остановки;
- полнота по факту;
- инкрементальный сбор;
- работа без обхода защит.

Есть хорошие образцы отдельных приёмов. Поэтому выбраны **собственные адаптеры** на стеке AuditLens (httpx, SQLAlchemy). Из чужих решений перенесены идеи, а не код: лицензии у большинства репозиториев отсутствуют, MIT — у немногих.

## 2ГИС

| Репозиторий | Язык | ★ | Изменён | Лицензия | Метод | Что даёт | Ограничения | Масштаб / интеграция |
|---|---|---|---|---|---|---|---|---|
| [yaroslaff/antifraud2gis](https://github.com/yaroslaff/antifraud2gis) | Python | 91 | 2026-11-02 | MIT | Публичный сервис отзывов `/2.0/branches/{id}/reviews`, ключ из настроек 2gis.ru | Полный JSON отзыва, ответ банка, публичные ленты авторов (антифрод) | `rated=true` теряет отзывы без оценки, потолок 500, без пауз | Очередь dramatiq/redis. Тяжёлое приложение — берём только идею ~60 строк |
| [interlark/parser-2gis](https://github.com/interlark/parser-2gis) | Python | 415 | 2024-04-21 | LGPL-3.0 | Chrome по CDP, перехват `items/byid` | Карточки организаций, без текстов отзывов | Медленно, открыт вопрос 2026 «антибот?» | Не библиотека, не поддерживается |
| [ruvn-1fgas/2gis-parser](https://github.com/ruvn-1fgas/2gis-parser) | Python, Scrapy | 6 | 2026-03-19 | нет | Старый каталог 2.0 с ключами из старых клиентов + сервис отзывов 3.0 | Каталог, отзывы, фото | 16 параллельных запросов, ротация прокси — не берём | Константы полезны, код — нет (без лицензии) |
| [Raibek885/2gis-review-parser](https://github.com/Raibek885/2gis-review-parser) | Python | 1 | 2026-03-19 | нет | Ключ из `__customcfg` страницы, сервис отзывов, **лента организации `/orgs/{id}/reviews`** | Отзывы отделения, организации, гео | Браузер для старта | Идея: ключ берётся со страницы, а не из кода |
| [KirillPugov/reviews-parser-2gis-YaMaps](https://github.com/KirillPugov/reviews-parser-2gis-YaMaps) | TypeScript | 0 | 2026-07-20 | MIT | Сервис отзывов 3.0, `next_link`, запасной `offset_date` | Отзывы и ответы, инкрементальная дозапись | Одно отделение за запуск | Лучшая защита пагинации (seen-cursor/ids) |
| [ABRAMOVI4CH/2gisReviewsParser](https://github.com/ABRAMOVI4CH/2gisReviewsParser) | TypeScript | 0 | 2026-02-16 | MIT | Сервис отзывов 3.0 | Отзывы, ответ, ссылка на отзыв | Нет пауз и повторов | npm-библиотека, просто перенести |
| [posander/2gis_reviews_collector](https://github.com/posander/2gis_reviews_collector) | Python | 0 | 2026-03-19 | нет | Сервис 3.0, `rated=true` и `rated=false` | Отзывы, ответ, признак скрытия | — | Подтвердил: без `rated` приходят оба вида сразу |
| [melaven/enterprise-serm-platform](https://github.com/melaven/enterprise-serm-platform) | Python | 11 | 2026-11-04 | не указана | httpx async, сервис отзывов | Отзывы без ответов | Ошибка: сортировка по правке и курсор по созданию | Ближе всех к нашему стеку; хорошая таксономия ошибок 403/429 → пауза |
| [alexunderlag/Review-Monitor-Bot](https://github.com/alexunderlag/Review-Monitor-Bot) | Python | 2 | 2026-03-17 | нет | Сервис 2.0, сортировка по правке | Новые отзывы в Telegram | Несколько отделений | Образец инкрементального мониторинга |
| [ivdenis111-tech/2gis-competitor-intel](https://github.com/ivdenis111-tech/2gis-competitor-intel) | Python | 0 | 2026-04-25 | нет | Playwright: перехват маркеров карты + сервис отзывов | Список организаций с координатами | Нужен браузер | Альтернатива поиску отделений |
| [dobrewoks1/2gis-collector](https://github.com/dobrewoks1/2gis-collector) | JavaScript | 0 | 2026-10-15 | MIT | **Официальный** каталог 3.0 со своим ключом, сетка точек | Полный каталог по городу | Ключ платный, отзывов нет | Законный путь к реестру при покупке ключа |
| [2gis/2gis-mcp](https://github.com/2gis/2gis-mcp) | TypeScript | 6 | 2026-10-16 | проприетарная | Официальный каталог | Поиск мест, без отзывов | — | Подтверждает: отзывы — не самообслуживаемый продукт |
| [theYahia/2gis-mcp](https://github.com/theYahia/2gis-mcp) | TypeScript | 4 | 2026-09-06 | MIT | Официальный каталог + отзывы «если получится» | — | Ключ разработчика к сервису отзывов отдаёт 401/403 | — |
| [khoshov/skillhub-data-collection](https://github.com/khoshov/skillhub-data-collection) | Python | 1 | 2023-03-01 | нет | Подсказки каталога + сервис отзывов | — | Устарел | Старый ключ сервиса отзывов тоже ещё работает |
| [tonatos/flamp-parser](https://github.com/tonatos/flamp-parser) | Python | 3 | 2017-11-02 | нет | Flamp API | — | flamp.ru за JS-проверкой | Мёртв. Отзывы Flamp уже есть в ленте 2ГИС (`provider=flamp`) |

## Яндекс Карты

| Репозиторий | Язык | ★ | Изменён | Лицензия | Метод | Ограничения | Вывод |
|---|---|---|---|---|---|---|---|
| [robert-werner/marketplace-maps-parser](https://github.com/robert-werner/marketplace-maps-parser) | Python 3.14 | 1 | 2026-11-03 | нет | Браузер со скрытием автоматизации + подписанный `fetchReviews`, 4 сортировки × 600 + аспекты | Нужен экран, пул прокси и подмена отпечатка — против наших правил | Лучший справочник по контракту сервиса; транспорт не берём |
| [volk6022/Atomic-Scraper-Service](https://github.com/volk6022/Atomic-Scraper-Service) | Python | 0 | 2026-07-04 | MIT | httpx: страницы `/reviews/?page=N` (данные страницы), поиск по сетке | 600, только релевантность (сортировка в адресе не работает — проверено) | Образец пути без подписи — так работает наш режим по умолчанию |
| [KirillPugov/reviews-parser-2gis-YaMaps](https://github.com/KirillPugov/reviews-parser-2gis-YaMaps) | TypeScript | 0 | 2026-07-20 | MIT | Без браузера: страница → csrf/session → `fetchReviews` с подписью `s` | Не знает про потолок 600 | Самая короткая рабочая реализация (~80 строк) |
| [no1621name/ymaps-parser](https://github.com/no1621name/ymaps-parser) | PHP | 0 | 2026-06-20 | нет | Подписанный `fetchReviews`, 12 страниц | Параллельность 3 — много | Сверка параметров |
| [s0loparty/parse-organizations](https://github.com/s0loparty/parse-organizations) | PHP | 0 | 2026-09-16 | нет | `fetchReviews` с подписью без загрузки страницы | Не проверено | Идея лёгкого старта сессии |
| [SamLatsin/all-web-crawlers](https://github.com/SamLatsin/all-web-crawlers) | Python | 0 | 2023-08-03 | MIT | Подпись `s`, вход под учётной записью | Учётная запись — против правил | Подпись не менялась с 2021 года |
| [useless-apple/yandex_reviews-parser](https://github.com/useless-apple/yandex_reviews-parser) | Python | 71 | 2024-09-09 | MIT | undetected_chromedriver, прокрутка | 600, хрупкие селекторы, маскировка | Не берём |
| [Alexandre77777/yandex-maps-reviews](https://github.com/Alexandre77777/yandex-maps-reviews) | Python | 0 | 2026-10-03 | MIT | Обычный Playwright без маскировки, «По новизне» | Залипает на 600 (подтверждено) | Хорошая таксономия статусов (complete / capped / stalled / captcha → стоп) |
| [KrivchenkoEgor/yandex-maps-reviews-scraper](https://github.com/KrivchenkoEgor/yandex-maps-reviews-scraper) | Python | 0 | 2026-08-29 | MIT | Playwright, перехват `fetchReviews`, «очеловечивание» | 600 из 1 750; после частых запусков — заглушка 158 байт | Заметки о ловушках полезны, маскировку не берём |
| [Meldren/ReviewCrawler](https://github.com/Meldren/ReviewCrawler) | Kotlin | 4 | 2025-04-10 | нет | ChromeDriver + CDP, «По новизне», стоп на известном отзыве | — | Образец инкрементального стопа |
| [arsenyvolodko/yandex-maps-reviews-parser](https://github.com/arsenyvolodko/yandex-maps-reviews-parser) | Python | 4 | 2024-01-12 | нет | Selenium; id организации — через платный Search API | Нет дат и id | Официальный поиск организаций — законный путь при покупке ключа |
| [filippchistiakov/YandexMapsReviewsParser](https://github.com/filippchistiakov/YandexMapsReviewsParser) | Python | 14 | 2025-08-01 | нет | Selenium Firefox, XPath | 600, ошибки | Не берём |
| [ABRAMOVI4CH/YandexReviewsParser](https://github.com/ABRAMOVI4CH/YandexReviewsParser) | TypeScript | 0 | 2026-02-14 | MIT | Puppeteer (вопреки описанию) | 600 | Не берём |
| [g4reev/JoyReviews](https://github.com/g4reev/JoyReviews) | Python | 1 | 2023-03-03 | MIT | Виджет отзывов + cfscrape | 5 отзывов, без года | Не берём (cfscrape — обход защиты) |

## Мультиисточниковые и смежные

| Репозиторий | Язык | ★ | Изменён | Лицензия | Что делает | Вывод для нас |
|---|---|---|---|---|---|---|
| [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper) | Go | 7 343 | 2026-10-21 | MIT | Google Карты: сначала браузер, запасной HTTP-путь к внутреннему `listugcposts`; расширенные отзывы до ~300 | Самый зрелый проект по Google. Google в задаче не основной — только если владелец решит добавить |
| [YasogaN/google-maps-review-scraper](https://github.com/YasogaN/google-maps-review-scraper) | TypeScript | 127 | 2026-10-21 | MIT | Внутренний `GetLocalBoqProxy`: 4 сортировки, ~10 отзывов на страницу | Проверено: отвечает без браузера. Это внутренний сервис, как подпись Яндекса, — нужно решение |
| [georgekhananaev/google-reviews-scraper-pro](https://github.com/georgekhananaev/google-reviews-scraper-pro) | Python | 346 | 2026-10-18 | MIT | Google, браузер | Не берём (браузер) |
| [gaspa93/googlemaps-scraper](https://github.com/gaspa93/googlemaps-scraper) | Python | 531 | 2026-11-02 | GPL-3.0 | Google, Selenium | Не берём (GPL, браузер) |
| [alltheplaces/alltheplaces](https://github.com/alltheplaces/alltheplaces) | Python | 849 | 2026-11-05 | смешанная | Пауки сайтов сетей: адреса и координаты точек | Паука Сбера нет: сайт Сбербанка закрыт F5. Идея пригодна для справочника banki.ru |
| [osmlab/name-suggestion-index](https://github.com/osmlab/name-suggestion-index) | JSON | 889 | 2026-11-05 | BSD-3 | Бренды OSM: Сбербанк — `brand:wikidata=Q205012` | Ключ для запроса к OSM |
| [mvexel/overpass-api-python-wrapper](https://github.com/mvexel/overpass-api-python-wrapper) | Python | 401 | 2026-01-08 | нет | Клиент Overpass | Не нужен: один запрос к Overpass делается и без него |
| [melaven/enterprise-serm-platform](https://github.com/melaven/enterprise-serm-platform) | Python | 11 | 2026-11-04 | не указана | 2ГИС + Яндекс + сайты отзывов, ARQ/redis | Разобран выше |
| [pyLexxDramma/unified_parser_v2](https://github.com/pyLexxDramma/unified_parser_v2), [GrigoriyKruchinin/reputa](https://github.com/GrigoriyKruchinin/reputa), [hive937/maps_reviews_bot](https://github.com/hive937/maps_reviews_bot) | Python | 0–1 | 2023–2026 | нет | Связки 2ГИС и Яндекса для мониторинга репутации | Ничего сверх уже разобранных приёмов |
| [findatalab/domclick](https://github.com/findatalab/domclick), [mpxx1/domclick-parser-rs](https://github.com/mpxx1/domclick-parser-rs), [joshixap/arenda](https://github.com/joshixap/arenda) | Python, Rust | 0 | 2024–2026 | нет | **Объявления** о недвижимости (`api.domclick.ru/catalog/offer`) | Отзывов об отделениях нет ни в одном |
| [scrubber-reviews/otzovik_com](https://github.com/scrubber-reviews/otzovik_com), [GlaticuS/irecommend-parser](https://github.com/GlaticuS/irecommend-parser), [yeqwer/parser_and_analyzer_banki.ru](https://github.com/yeqwer/parser_and_analyzer_banki.ru), [Ortant333/ortant-banks](https://github.com/Ortant333/ortant-banks) | Python | 0–2 | 2021–2026 | разные | Отзывы на уровне банка | Не про отделения. banki.ru и sravni уже есть в AuditLens |
| [tonatos/flamp-parser](https://github.com/tonatos/flamp-parser) | Python | 3 | 2017 | нет | Flamp | Мёртв. Отзывы Flamp приходят в ленте 2ГИС |

Итог по разделу:
- Google — единственный крупный источник по отделениям, который мы не берём. Официальный Places API отдаёт не больше 5 отзывов на точку. Полный объём — только внутренние сервисы, так что это такое же решение владельца, как подпись Яндекса.
- Готовых парсеров Домклика с отзывами об отделениях нет: на Домклике таких отзывов нет.
- Для справочника полезны OSM (бренд Q205012) и справочник офисов banki.ru (код ВСП и координаты), а не чужие пауки.

## Что выгоднее: библиотека, адаптация или свои коннекторы

| Вариант | За | Против |
|---|---|---|
| Готовая библиотека | — | Ни одна не умеет реестр, продолжение и полноту. У большинства нет лицензии. Самые «популярные» по Яндексу держатся на маскировке браузера |
| Адаптировать проект (antifraud2gis, enterprise-serm) | Есть очередь задач и кэш | Чужая архитектура (redis, LMDB, ARQ), ошибки пагинации, лишние зависимости |
| **Свои адаптеры** (выбрано) | Единый интерфейс, своя модель полноты, стек AuditLens, ~1 000 строк, тесты без сети | Поддерживать самим. Сервисы недокументированы и могут меняться — нужен мониторинг блокировок |

Перенесённые идеи:
- ключ 2ГИС со страницы, а не из кода;
- без `rated` — полная выдача 2ГИС;
- лента организации 2ГИС для ежедневной дельты;
- защита пагинации через уже виденные курсоры и id;
- таксономия статусов полноты;
- остановка при капче;
- пауза на 403/429 с учётом `Retry-After`;
- стоп «только новых» на известном отзыве.

Не перенесено:
- маскировка браузера (undetected-chromedriver, stealth, «очеловечивание»);
- ротация прокси;
- cfscrape;
- учётные записи;
- старые ключи каталога 2ГИС;
- подпись каталога 2ГИС.
