# Сборщик отзывов о ВСП: запуск

## Локально

```bash
cd al-reviews                      # корень репозитория AuditLens
export PYTHONPATH=src
export VSP_DB_URL=sqlite:///workspace/vsp/vsp_reviews.sqlite   # по умолчанию так и есть
export VSP_AUTHOR_SALT=$(cat workspace/vsp/.author_salt)       # соль для ссылок на авторов, не коммитить

# пилот: реестр ЦБ → 2ГИС → Яндекс → привязка → сбор → отчёт (~30 отделений)
.venv/bin/python -m bank_audit.vsp pilot --out workspace/vsp/pilot

# пилот прервался после 2ГИС — продолжить с шага Яндекса (реестр и 2ГИС из pilot_selection.json)
.venv/bin/python -m bank_audit.vsp pilot --out workspace/vsp/pilot --from-step yandex

# по шагам
.venv/bin/python -m bank_audit.vsp discover --source 2gis --city Казань
.venv/bin/python -m bank_audit.vsp match
.venv/bin/python -m bank_audit.vsp sync --source 2gis --city Казань            # полный проход
.venv/bin/python -m bank_audit.vsp sync --source 2gis --city Казань --mode new # только новые
.venv/bin/python -m bank_audit.vsp sync --source yandex --id 1050330773        # одно отделение
.venv/bin/python -m bank_audit.vsp report --out workspace/vsp/COMPLETENESS.md
.venv/bin/python -m bank_audit.vsp export --out workspace/vsp/reviews.jsonl --source 2gis
```

Тесты: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/vsp`. Сеть они не используют.

## Настройки (переменные окружения)

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `VSP_DB_URL` | `sqlite:///workspace/vsp/vsp_reviews.sqlite` | Своя база модуля; на сервере — `postgresql+psycopg://…` |
| `VSP_AUTHOR_SALT` | `change-me` | Соль хэша автора. Задать своё значение, не менять после начала сбора |
| `VSP_USER_AGENT` | актуальный Chrome | Со старой версией 2gis.ru отдаёт заглушку `/museum` |
| `VSP_LOG_DIR` | `workspace/vsp/logs` | `vsp.log` |
| `VSP_2GIS_RPS` / `VSP_2GIS_CONC` | 1.0 / 2 | Частота к 2ГИС и число отделений одновременно |
| `VSP_YANDEX_RPS` / `VSP_YANDEX_CONC` | 0.3 / 1 | Частота к Яндексу |
| `VSP_YANDEX_SIGNED_API` | 0 | 1 — внутренний сервис отзывов Яндекса (подпись запроса как в коде страницы): «сначала новые» и объединение 4 сортировок. **Включает владелец** |
| `VSP_2GIS_REVIEW_KEY` | — | Ручная замена ключа сервиса отзывов (обычно берётся со страницы 2gis.ru) |
| `VSP_{2GIS,YANDEX,DOMCLICK}_ENABLED` | 1 / 1 / 0 | Выключить источник |

## На сервере

1. Код приезжает обычной выкладкой. Модуль лежит в образе, но приложение его не запускает.
2. База — отдельная. Вариантов два:
   - отдельная база PostgreSQL (`VSP_DB_URL=postgresql+psycopg://…/vsp`) — рекомендуется;
   - отдельная схема в основной базе.

   Таблицы создаются при первом запуске. Для основной базы AuditLens — только с `lock_timeout` и по согласованию.
3. Запуск отдельным процессом в контейнере, не из планировщика приложения:

   ```bash
   docker exec -d -e VSP_DB_URL=… -e VSP_AUTHOR_SALT=… auditlens-app \
     sh -lc 'cd /app && python -m bank_audit.vsp sync --source 2gis --city Казань --mode new >> /tmp/vsp.out 2>&1'
   ```

4. Регулярный режим (после согласования):
   - каждую ночь: `--mode new` по 2ГИС и Яндексу;
   - раз в неделю: `--mode full`, чтобы обновить ответы банка, правки и пропавшие отзывы;
   - раз в месяц: `discover` и реестр ЦБ, `match`.

## Требует согласования

- **Массовый прогон по всей сети** (`sync --all`). Защищён флагом `--i-know-this-is-mass`.
- **`VSP_YANDEX_SIGNED_API=1`.** Сбор через внутренний сервис Яндекса с подписью запроса. По письму поддержки Яндекса способы сообщества не запрещены; решение за владельцем.
- **Запуск с IP прода.** Если источник начнёт ограничивать доступ, ограничение коснётся адреса, с которого работают и другие сборщики AuditLens.
- **Платный геокодер или официальный API 2ГИС.** Нужен, если понадобятся координаты для тех примерно 4% адресов ЦБ, которые не разобрались, или полный каталог 2ГИС.

## Что делать, если…

- **`blocked` в отчёте.** Источник показал капчу или 401/403. Сборщик остановил источник в этом прогоне. Повторить позже и с меньшей частотой (`VSP_*_RPS`). Обходить защиту не нужно.
- **`below_declared` у 2ГИС.** Источник заявляет больше, чем отдаёт. Обычно это скрытые модерацией отзывы, они не видны никому.
- **`window` у Яндекса.** У отделения больше 600 отзывов, по умолчанию берём 600. Режим с подписью даёт около 68%.
- **Прерван прогон.** Запустить ту же команду: незавершённые отделения продолжатся с сохранённого курсора.
