# hermes-al — агент Hermes для ИИ-аналитика AuditLens

Отдельная инсталляция Nous Hermes Agent в своём контейнере (`hermes-al`, дом
`/root/.hermes` в volume `hermes_al_home`, API на 127.0.0.1:8642). С любыми
другими инсталляциями Hermes на машине не связана.

## Как устроено

| Слой | Где | Что задаёт |
|---|---|---|
| Харнесс | Hermes Agent | цикл с инструментами, навыки, память, самообучение, куратор |
| Кто агент и как отвечает | `SOUL.md` | роль, честность, формат ответа |
| Методика по областям | `skills/auditlens-*` | жалобы, рынок, документы и новости, база данных |
| Данные | MCP-сервер приложения `/mcp/` (`ai/mcp_server.py`, `ai/agent_tools.py`) | те же функции, что рисуют вкладки; инструменты `mcp__auditlens__*` |
| Проводка вопроса и ответа | `ai/hermes_quick.py` | контекст прогона, чистка ответа, повтор при пустом финале |
| Замер качества | `ai/agent_eval.py`, `learn_gate.sh` | регрессионный набор, откат навыков при деградации |

Штатные инструменты Hermes (терминал, файлы, код, веб, память, навыки) остаются
включены. `alsql` в терминале — psql-обёртка к базе (DELETE/DROP/ALTER/TRUNCATE
запрещены); образец — `alsql.example`.

**Самообучение.** Агент сам пишет и правит навыки (`skills.write_approval: false`),
куратор раз в неделю сливает похожие и архивирует неиспользуемые
(`curator.consolidate: true`). Страховка — `learn_gate.sh`.

## Выкатка настроек

```bash
# на ВМ: скопировать deploy/hermes-al в ~/hermes-al/repo, затем
bash ~/hermes-al/repo/sync.sh --dry-run
bash ~/hermes-al/repo/sync.sh
```

Скрипт: резервная копия (`~/hermes-al/backups`), конфиг с ключом модели из
действующего, ключ MCP в окружение Hermes, SOUL/навыки/память, архив навыков
с вредными приёмами, сверка размеров файлов в контейнере, перезапуск, проверка
наборов инструментов и навыков. В конце печатает команду отката.

Ключ MCP — `AGENT_MCP_KEY` в `.env` приложения; без него MCP-сервер выключен.
Сервер принимает только прямые локальные запросы с этим ключом.

## Выбор модели

`platforms.api_server.extra.model_routes` — маршруты моделей: поле `model` в
`POST /v1/runs` выбирает модель на один прогон. Сравнение на регрессионном наборе:

```bash
docker exec auditlens-app auditlens agent-eval --model sonnet
```

Модель по умолчанию для чата — `model.default` в `config.yaml` или env
`HERMES_MODEL` приложения (маршрут).

## Проверка самообучения

`learn_gate.sh` — cron раз в неделю: прогон набора; не хуже эталона −15 →
снимок навыков становится «последним хорошим»; хуже → навыки и память
откатываются к снимку (упавшие — в архив), прогон повторяется. Итоги — в
«Пульсе», лог — `~/hermes-al/gate/gate.log`.

## Сборка контейнера с нуля

```bash
docker build -t hermes-al .
docker run -d --name hermes-al --network host --restart unless-stopped \
  --env-file env.container -v hermes_al_home:/root/.hermes -v hermes_al_ws:/root/workspace hermes-al
bash sync.sh
```
