# Как получить API-ключи

AuditLens работает на одном **обязательном** ключе (LLM-провайдер) и нескольких **опциональных** (для расширенного web-поиска). Ниже — пошаговые инструкции.

> 💡 Все ключи кладутся в файл `.env` в корне репозитория. Этот файл игнорируется git'ом — секреты не утекут.

---

## 1. Fireworks AI — основной LLM-провайдер (обязательно)

**Что это:** OpenAI-совместимый endpoint с моделями GPT-OSS-120B, Kimi K2, DeepSeek V4, GLM 5, Llama и др. Главные плюсы для аудитора в РФ:

- ✅ **$15 бесплатных кредитов** при регистрации (≈ 30–50 deep-research'ей)
- ✅ Подключение без VPN из РФ
- ✅ OpenAI-совместимый API — работает с любым кодом написанным под OpenAI SDK
- ✅ Дешевле OpenAI/Anthropic в 3-5 раз
- ✅ Reasoning-модели и обычные доступны параллельно

### Шаги получения ключа

1. **Зарегистрируйся:** [https://fireworks.ai/](https://fireworks.ai/) → `Sign Up`
   - Email + пароль ИЛИ Google/GitHub. Без верификации карты.
   - При регистрации сразу зачислится $15 бесплатных кредитов.

2. **Создай API-ключ:**
   - Перейди в [API Keys](https://fireworks.ai/account/api-keys)
   - `Create API Key` → дай имя (например `auditlens-dev`) → `Create`
   - **Скопируй ключ сразу** — он показывается только один раз. Формат: `fw_<длинная_строка>`

3. **Положи ключ в `.env`:**
   ```bash
   LLM_API_KEY=fw_твой_ключ_здесь
   ```

4. **Проверь баланс:** [https://fireworks.ai/account/billing](https://fireworks.ai/account/billing) — ты должен увидеть «$15.00 credit».

### Какую модель выбрать

По умолчанию настроена `gpt-oss-120b` — оптимальный баланс скорости и качества для русского языка. Альтернативы (если хочешь поэкспериментировать):

| Модель | Контекст | Русский | Скорость | Когда выбирать |
|---|---|---|---|---|
| `gpt-oss-120b` (default) | 131k | ★★★ | ⚡⚡⚡ быстро (~30s) | Универсальный выбор |
| `glm-5p1` | 203k | ★★★★ | ⚡⚡ средне (~30-60s) | Лучшее качество русского |
| `deepseek-v4-pro` | 1M | ★★★★★ | ⚡ медленно (~2min) | Когда нужен максимум контекста (длинные PDF) |
| `kimi-k2p6` | 262k | ★★★ | ⚡⚡ средне | Если doc больше 200k — `glm-5p1` или `kimi` |

Меняется одной строкой в `.env`:
```bash
LLM_MODEL_NAME=accounts/fireworks/models/glm-5p1
```

> ⚠️ Reasoning-модели (kimi, deepseek, glm) пишут «рассуждения» в `content` — AuditLens автоматически вырезает их через `<answer>`-обёртку. Дополнительной настройки не требуется.

---

## 2. SearXNG — мета-поисковик (опционально, рекомендуется)

**Что это:** self-hosted мета-агрегатор Google/Bing/DDG/Brave/Qwant/Mojeek/Yandex. Безлимит, бесплатно, без API-ключей.

### Зачем

Без SearXNG AuditLens упадёт в DuckDuckGo / Yandex (часто 403/captcha). С SearXNG — стабильно 7 движков с автоматическим fallback'ом.

### Запуск

Уже включён в `docker-compose.yml`. Достаточно:
```bash
docker compose up -d searxng
curl 'http://localhost:8888/search?q=сбербанк&format=json' | head -50
```

Если запустил — `SEARXNG_URL=http://localhost:8888` в `.env` уже работает.

---

## 3. Brave Search API — резервный поисковик (опционально)

**Что это:** независимый поисковый индекс (не Google/Bing). Хорошее качество, специально для разработчиков.

- ✅ **2000 запросов/месяц бесплатно** (хватит за глаза)
- ✅ Регистрация без карты
- ✅ Без VPN из РФ

### Шаги

1. [https://api.search.brave.com/app/keys](https://api.search.brave.com/app/keys) → `Sign Up`
2. `Add Subscription` → выбери `Free` план
3. `Create new API key` → скопируй ключ (формат `BSA...`)
4. В `.env`:
   ```bash
   BRAVE_SEARCH_API_KEY=BSA_твой_ключ
   ```

Без Brave AuditLens работает (использует SearXNG → DDG → Yandex), но Brave даёт +1 надёжный источник.

---

## 4. Anthropic (Claude) — альтернатива Fireworks (опционально)

Если хочешь использовать Claude вместо Fireworks:

1. [https://console.anthropic.com/](https://console.anthropic.com/) → Sign Up
2. Add payment method → Get API key
3. В `.env`:
   ```bash
   LLM_BASE_URL=https://api.anthropic.com/v1
   LLM_API_KEY=sk-ant-...
   LLM_MODEL_NAME=claude-sonnet-4-5-20250929
   ```

> ⚠️ Claude API из РФ — только через VPN. Fireworks работает напрямую.

---

## 5. Russian Trusted Root CA (для сайтов Сбера)

Сбербанк использует TLS-сертификаты от Russian Trusted Root CA (Минцифры). Без них `httpx` будет ругаться на `CERTIFICATE_VERIFY_FAILED`.

Решение **уже встроено** в репозиторий:
- `config/russian_trusted_root.pem` — сертификат
- `config/ca_bundle_combined.pem` — объединён с certifi
- `bank_audit/rag/fetcher.py` — использует bundle автоматически

Никаких действий не требуется. Сертификат публичный, выложен в репо.

---

## FAQ

**Q: Можно ли использовать AuditLens без LLM-ключа?**
A: Нет. LLM нужен для resolver / planner / synthesizer / charts. Это основа Deep Research. Без него работают только сырые tools (semantic_search, get_market_offers и т.п.) через CLI.

**Q: Куда уходят запросы — в США / Китай?**
A: Fireworks AI хостится в США (датацентры в Орегоне). Если это критично — используй [self-hosted vLLM](https://github.com/vllm-project/vllm) с GPU-инстансом в РФ и подмени `LLM_BASE_URL=http://your-vllm-server:8000/v1`. Промпты содержат только публичную фактуру (название банка, продукт, найденные документы) — не содержат внутренних данных.

**Q: Что делать когда закончатся $15?**
A: Fireworks billing → положить любую сумму ($5 минимум). Реальная цена 1 deep-research'а ≈ $0.10-0.20 на gpt-oss-120b. $5 = 25-50 запросов.

**Q: Можно ли мониторить расходы?**
A: Да — [https://fireworks.ai/account/billing](https://fireworks.ai/account/billing) показывает usage по дням и моделям.
