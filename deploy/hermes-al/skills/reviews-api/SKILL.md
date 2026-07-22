# reviews-api: аналитика жалоб клиентов (390 тыс. отзывов banki.ru)

Готовые агрегаты через локальный API AuditLens (быстрее и надёжнее ручного SQL):
- curl -s "http://127.0.0.1:8000/api/reviews/overview?bank=Сбербанк&days=90"  — объём, дельта, доля рынка
- curl -s "http://127.0.0.1:8000/api/reviews/themes?bank=Сбербанк"            — темы жалоб с рисками
- curl -s "http://127.0.0.1:8000/api/reviews/trend?bank=Сбербанк"             — тренд по месяцам
- curl -s "http://127.0.0.1:8000/api/reviews/vs-market?bank=Сбербанк"         — против рынка
- curl -s "http://127.0.0.1:8000/api/reviews/products?bank=Сбербанк"          — точные метки продуктов
- curl -s "http://127.0.0.1:8000/api/reviews/feed?bank=Сбербанк&q=слово&limit=10" — конкретные жалобы
Параметр product= — ТОЛЬКО точная метка из /products (например «Кредитная карта»).
URL-энкодь кириллицу. В ответе указывай период и n.
