"""AI Analyst: OpenAI-compatible LLM с function calling для ответов по данным.
   Стримит ответ через async generator → SSE.

   Конфиг через env-переменные:
     LLM_BASE_URL   — напр. https://api.fireworks.ai/inference/v1
     LLM_API_KEY    — API-ключ провайдера
     LLM_MODEL_NAME — напр. accounts/fireworks/models/llama-v3p3-70b-instruct
"""
from __future__ import annotations
import asyncio, json, os, logging, time
from typing import AsyncIterator
from openai import AsyncOpenAI
from sqlalchemy import text
from .. import db
from ..clock import today_anchor

log = logging.getLogger(__name__)

# ── LLM конфиг из env ──────────────────────────────────────────────────────
LLM_BASE_URL   = os.getenv("LLM_BASE_URL",   "https://api.openai.com/v1")
LLM_API_KEY    = os.getenv("LLM_API_KEY",    os.getenv("OPENAI_API_KEY", ""))
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "gpt-4o")

# Hybrid split: fast для рутины (planner/resolver/charts/agent-gap), smart —
# для глубокого чтения источников (fact-extract/synth/critic/merge/addendum).
# Если env не задан — fallback на LLM_MODEL_NAME (zero breaking-change).
LLM_MODEL_FAST  = os.getenv("LLM_MODEL_FAST",  LLM_MODEL_NAME)
LLM_MODEL_SMART = os.getenv("LLM_MODEL_SMART", LLM_MODEL_NAME)
# Отдельный тир для аналитики вкладок «Обзор» и «Отзывы» (поиск скрытых
# паттернов в жалобах, курирование дайджеста). Умнее smart, но НЕ трогает
# deep-research (у него свой smart). Не задан → падаем на smart_model.
LLM_MODEL_INSIGHT = os.getenv("LLM_MODEL_INSIGHT", "")


def smart_model() -> str:
    """Модель для задач требующих глубокого reasoning над источниками."""
    return LLM_MODEL_SMART or LLM_MODEL_NAME


def fast_model() -> str:
    """Модель для рутинных задач (короткий JSON-output, structured)."""
    return LLM_MODEL_FAST or LLM_MODEL_NAME


def insight_model() -> str:
    """Модель для аналитики «Обзора»/«Отзывов» (скрытые паттерны, сводки).
    Env LLM_MODEL_INSIGHT (напр. anthropic/claude-sonnet-4.6); иначе — smart."""
    return LLM_MODEL_INSIGHT or smart_model()

SYSTEM = """Ты — аналитик службы внутреннего аудита Сбербанка, отдел розничного бизнеса.
У тебя есть доступ к knowledge layer:
  • Структурированный слой (горячий): SQL по offers/reviews/quality_flag через run_sql, get_market_offers и др.
  • Тёплый слой: pre-indexed документы (banki_official, регуляторы, агрегаторы) через semantic_search
  • Холодный слой (real-time): fetch_official для свежих официальных страниц банков
  • Жалобы клиентов (ОСНОВНОЕ): search_complaints — корпус banki.ru (~390к
    реальных негативных отзывов 2025-2026, с датами/ссылками)
  • Горячий слой отзывов (агрегаты): get_review_themes — топ тем/sentiment per bank

Стратегия выбора инструмента:
  • Точные числа (ставки, лимиты сумм) → run_sql, get_market_offers
  • Описательные сравнения (фичи, условия, тарифы, услуги) → semantic_search
  • Если semantic_search дал <3 результата ИЛИ данные могут быть устаревшими → fetch_official
  • Жалобы / проблемы / риски по банку → search_complaints (реальные отзывы),
    затем при нужде get_review_themes для агрегатов
  • Можешь вызывать НЕСКОЛЬКО tools последовательно, чтобы сложить полную картину

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА цитирования (как Perplexity):
  • Если используешь данные из semantic_search или fetch_official — ставь маркер [N] прямо в текст
  • [1], [2], [3] — порядковые номера ИСТОЧНИКОВ, которые система сама подставит в sources panel
  • НИКОГДА не придумывай факты — цитируй только то, что нашёл в инструментах
  • Если данных не нашлось — честно скажи "по доступным источникам данных нет"

Стиль ответа:
  • Русский язык, коротко и по делу
  • Markdown заголовки, таблицы для сравнений
  • При сравнении всегда выделяй позицию Сбера относительно рынка
  • Указывай аномалии и подводные камни
  • Для числовых данных — единицы измерения
  • Точка зрения — аудитор Сбера: другие банки это бенчмарк, а не объект выбора.
    НЕ советуй «перейти/закупить/оформить у конкурента» и не давай советов как
    клиенту — оцениваешь риски и позицию Сбера."""

# ── Инструменты (OpenAI function-calling формат) ───────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_market_offers",
            "description": "Получить текущие рыночные предложения по категории продукта. Возвращает список банков со ставками и условиями.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["deposit", "credit", "card_credit", "card_debit", "mortgage", "auto_loan", "metals", "other"],
                        "description": "Категория банковского продукта"
                    },
                    "limit": {"type": "integer", "default": 20, "description": "Максимальное количество записей"}
                },
                "required": ["category"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_sber_vs_market",
            "description": "Сравнение предложений Сбербанка с рынком по всем категориям. Показывает разницу в ставках (в п.п.).",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_reviews_analysis",
            "description": "Получить анализ отзывов: темы жалоб, sentiment, средний рейтинг для конкретного банка или всех банков.",
            "parameters": {
                "type": "object",
                "properties": {
                    "bank_slug": {
                        "type": "string",
                        "description": "Слаг банка (sberbank, vtb, tinkoff и т.д.) или 'all' для всех банков"
                    }
                },
                "required": ["bank_slug"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_complaints",
            "description": (
                "Реальные жалобы клиентов из корпуса banki.ru (~390к негативных "
                "отзывов 1-2★ за 2025-2026, 217 банков, с датами и ссылками). "
                "ОСНОВНОЙ источник жалоб (полнее агрегатов get_reviews_analysis).\n"
                "Главный режим: передай ТОЛЬКО bank (и product при наличии) БЕЗ "
                "query — увидишь, на что РЕАЛЬНО жалуются клиенты, не угадывая "
                "проблему. query задавай лишь для точечного среза по теме.\n"
                "⚠ Конкретный банк → передай bank. СРАВНЕНИЕ/ТОП банков → передай "
                "banks=[…] ИМЕННО те банки, что в вопросе пользователя (или явно "
                "уточни у него) — НЕ выдумывай произвольные; инструмент сделает "
                "точечный поиск по каждому и вернёт by_bank. Запрос query БЕЗ "
                "bank/banks — лишь общий рыночный top-k, он НЕ покрывает все банки и "
                "НЕ доказывает, что у банка нет жалоб."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "bank": {"type": "string", "description": "Имя ОДНОГО банка (Сбербанк/ВТБ/Т-Банк/…)"},
                    "banks": {"type": "array", "items": {"type": "string"}, "description": "Список банков для СРАВНЕНИЯ/ТОПа — поиск по каждому отдельно, ответ by_bank"},
                    "product": {"type": "string", "description": "Продукт banki.ru (опц.): «Вклад», «Кредитная карта», «Ипотека», «Обслуживание юридических лиц» (эквайринг/РКО)…"},
                    "query": {"type": "string", "description": "ОПЦИОНАЛЬНО — конкретная тема для точечного среза. Для общего обзора не задавай."},
                    "k": {"type": "integer", "default": 12},
                },
                "required": [],
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_bank_ratings",
            "description": "Получить рейтинги банков с banki.ru: средняя оценка, кол-во отзывов, % решённых обращений, место в рейтинге.",
            "parameters": {
                "type": "object",
                "properties": {
                    "top_n": {"type": "integer", "default": 15, "description": "Количество банков"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_change_history",
            "description": "История изменений условий предложений — что и когда менялось у банков.",
            "parameters": {
                "type": "object",
                "properties": {
                    "bank_slug": {"type": "string", "description": "Слаг банка или 'all'"},
                    "limit": {"type": "integer", "default": 20}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_review_themes",
            "description": (
                "Получить расширенный анализ отзывов конкретного банка: "
                "топ-5 жалоб + топ-3 похвалы с примерами цитат, sentiment counts, "
                "распределение по источникам (banki/sravni/bankiros). "
                "Период: 'all' | 'last_30d' | 'last_90d'. "
                "Используй для аудиторских вопросов вида 'основные проблемы Сбера за квартал'. "
                "Возвращает структуру review_summary — это уже агрегированные данные, "
                "не миллион сырых отзывов."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "bank_slug": {"type": "string", "description": "Слаг банка"},
                    "period":    {"type": "string",
                                  "enum": ["all", "last_30d", "last_90d"],
                                  "default": "all"}
                },
                "required": ["bank_slug"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_bank_features",
            "description": (
                "Получить структурированные факты о банке/компании (revenue, profit, market_share, "
                "ebitda и т.п.) которые уже были извлечены из документов и сохранены в БД. "
                "Это точные числа — используй их вместо повторного синтеза из текста. "
                "Возвращает массив фактов с claim_text цитатой и source URL. "
                "Если для банка нет фактов — возвращает []."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "bank_slug": {"type": "string"},
                    "feature_key": {"type": "string",
                        "description": "Опциональный фильтр: revenue / net_profit / market_share / ebitda / employees / mau"},
                    "year": {"type": "integer"}
                },
                "required": ["bank_slug"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "semantic_search",
            "description": (
                "Семантический поиск по pre-indexed документам банков (тёплый слой). "
                "Возвращает релевантные фрагменты текста из официальных сайтов банков, "
                "PDF тарифов, отзывов и т.п. с указанием источника и trust_score. "
                "Используй для вопросов вида 'какие условия SWIFT в Альфе', "
                "'есть ли мобильное приложение у X' — когда нужен текст, не структурированные числа. "
                "Если результатов мало (<3) — попробуй fetch_official для свежих данных."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query":       {"type": "string", "description": "Поисковый запрос"},
                    "bank_slugs":  {"type": "array", "items": {"type": "string"},
                                    "description": "Опциональный фильтр по банкам"},
                    "doc_types":   {"type": "array", "items": {"type": "string"},
                                    "description": "Опциональный фильтр: html|pdf|xlsx|pptx"},
                    "trust_min":   {"type": "number", "default": 0.5,
                                    "description": "Минимальный trust_score источника (0..1)"},
                    "top_k":       {"type": "integer", "default": 6}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_official",
            "description": (
                "Холодный fetch: загружает страницу официального сайта банка (или PDF/Excel), "
                "парсит и сохраняет в knowledge index — для следующих запросов будет в тёплом слое. "
                "Используй когда semantic_search не дал результата или данные могут быть устаревшими. "
                "Можно дать: (a) явный URL, или (b) bank_slug + topic — система найдёт URL "
                "сама (через bank_profile.key_pages или поиск по sitemap). "
                "ВАЖНО: passive enrichment — каждый вызов обогащает базу для будущих запросов."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url":       {"type": "string", "description": "Прямой URL (опционально)"},
                    "bank_slug": {"type": "string", "description": "Слаг банка"},
                    "topic":     {"type": "string",
                                  "description": "transfers | transfers_intl | deposits | "
                                                  "credits | mortgage | cards | tariffs | "
                                                  "support | mobile_app | premium"},
                    "query":     {"type": "string",
                                  "description": "Уточняющий вопрос для in-memory ranking фрагментов"},
                    "use_browser": {"type": "boolean", "default": False,
                                    "description": "true для SPA-сайтов где HTTP не работает"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Выполнить read-only SELECT-запрос на read-only views. "
                "Используй когда стандартные tools не подходят: для произвольной агрегации, "
                "join'ов, фильтров. ЗАПРЕЩЕНО: INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/TRUNCATE/COPY. "
                "Доступные views: "
                "v_offer_current(bank_slug, bank_name, is_sber, offer_id, category, title, "
                "rate_pct, rate_kind, currency, amount_min, amount_max, term_months_min, "
                "term_months_max, fee_open, fee_service, early_withdraw, capitalization, "
                "replenishable, conditions, valid_from, url), "
                "v_sber_vs_market(category, sber_max, sber_min, market_median, market_max, "
                "market_min, sber_vs_median_pp), "
                "v_offer_top_by_rate(bank_name, bank_slug, is_sber, category, title, rate_pct, "
                "term_months_min, amount_min, rk), "
                "v_review_topics(bank_slug, bank_name, topic, n, avg_rating), "
                "v_review_sentiment_share(bank_slug, bank_name, label, n, total, share), "
                "v_bank_coverage. "
                "Также можно SELECT из bank, review (text, rating, posted_at, status), "
                "quality_flag (severity, code, detail, created_at), extraction_run, change_history."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "Один SELECT-запрос. Без точки с запятой в конце. LIMIT обязателен (не более 200 строк)."
                    }
                },
                "required": ["sql"]
            }
        }
    }
]


# ── run_sql safety: разрешены только SELECT на whitelist таблиц/вью ─────────
_ALLOWED_RELATIONS = {
    "v_offer_current", "v_sber_vs_market", "v_offer_top_by_rate",
    "v_review_topics", "v_review_sentiment_share", "v_bank_coverage",
    "bank", "review", "review_topic", "review_sentiment",
    "product_offer", "product_terms", "quality_flag", "extraction_run",
    "change_history",
}
_FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "copy", "grant", "revoke", "vacuum", "merge", "call", "comment on",
    "do ", "set ", "lock ", "begin", "commit", "rollback",
)
_MAX_ROWS = 200


def _validate_sql(sql: str) -> str | None:
    """Возвращает None если sql безопасен, иначе строку с причиной отказа."""
    s = sql.strip().rstrip(";").strip()
    low = s.lower()
    if not low.startswith("select") and not low.startswith("with"):
        return "Только SELECT/WITH-запросы разрешены"
    # Запрещённые ключевые слова — отдельно стоящие
    import re as _re
    for kw in _FORBIDDEN_KEYWORDS:
        if _re.search(rf"\b{_re.escape(kw.strip())}\b", low):
            return f"Запрещённое ключевое слово: {kw.strip()}"
    if ";" in s:
        return "Запрещены multi-statement (точка с запятой)"
    # Проверка таблиц/вьюх: парсинг наивный, ищем FROM/JOIN <ident>
    refs = set(_re.findall(r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)", low))
    forbidden_refs = refs - _ALLOWED_RELATIONS
    if forbidden_refs:
        return f"Запрещённые таблицы/вьюхи: {', '.join(sorted(forbidden_refs))}"
    # Принудительный LIMIT — если не указан, оборачиваем
    return None


def _run_sql_safe(sql: str) -> str:
    err = _validate_sql(sql)
    if err:
        return json.dumps({"error": err}, ensure_ascii=False)
    s = sql.strip().rstrip(";").strip()
    # Принудительный LIMIT — оборачиваем в подзапрос если нет
    import re as _re
    if not _re.search(r"\blimit\s+\d+", s.lower()):
        s = f"SELECT * FROM ({s}) _q LIMIT {_MAX_ROWS}"
    try:
        with db.session() as sess:
            # Read-only гарантия на уровне транзакции
            sess.execute(text("SET TRANSACTION READ ONLY"))
            rows = sess.execute(text(s)).mappings().all()
    except Exception as e:
        return json.dumps({"error": f"sql_error: {e}"}, ensure_ascii=False)
    out = [dict(r) for r in rows]
    return json.dumps({"rows": out, "row_count": len(out)},
                      ensure_ascii=False, default=str)


def _run_tool(name: str, args: dict) -> str:
    with db.session() as s:
        if name == "get_market_offers":
            rows = s.execute(text("""
                SELECT bank_name, title, rate_pct, rate_kind, currency,
                       amount_min, amount_max, term_months_min, term_months_max,
                       fee_open, conditions,
                       CASE WHEN is_sber THEN 'СБЕР' ELSE '' END sber_mark
                  FROM v_offer_current
                 WHERE category = :c
                 ORDER BY rate_pct DESC NULLS LAST
                 LIMIT :l
            """), {"c": args["category"], "l": args.get("limit", 20)}).mappings().all()
            return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)

        if name == "get_sber_vs_market":
            rows = s.execute(text("""
                SELECT category, sber_max, sber_min, market_median,
                       market_max, market_min, sber_vs_median_pp
                  FROM v_sber_vs_market ORDER BY category
            """)).mappings().all()
            return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)

        if name == "get_reviews_analysis":
            slug = args["bank_slug"]
            bank_filter = "AND b.slug = :s" if slug != "all" else ""
            topics = s.execute(text(f"""
                SELECT b.name bank_name, rt.topic, count(*) n,
                       round(avg(r.rating),2) avg_rating
                  FROM review r JOIN bank b USING(bank_id)
                  JOIN review_topic rt USING(review_id)
                 WHERE 1=1 {bank_filter}
                 GROUP BY b.name, rt.topic ORDER BY n DESC LIMIT 30
            """), {"s": slug} if slug != "all" else {}).mappings().all()
            sentiment = s.execute(text(f"""
                SELECT b.name, rs.label, count(*) n,
                       round(avg(r.rating),2) avg_r
                  FROM review r JOIN bank b USING(bank_id)
                  LEFT JOIN review_sentiment rs USING(review_id)
                 WHERE 1=1 {bank_filter}
                 GROUP BY b.name, rs.label ORDER BY b.name, n DESC
            """), {"s": slug} if slug != "all" else {}).mappings().all()
            return json.dumps({
                "topics": [dict(r) for r in topics],
                "sentiment": [dict(r) for r in sentiment]
            }, ensure_ascii=False, default=str)

        if name == "search_complaints":
            from ..rag import bankiru_reviews as _br
            _q = (args.get("query") or "").strip() or None
            _k = int(args.get("k", 12))
            if not _q:
                _k = max(_k, 15)   # discovery — больше отзывов, чтобы видеть темы
            # СРАВНЕНИЕ/ТОП банков — точечно по каждому (надёжнее общего семантического)
            _banks = args.get("banks")
            if isinstance(_banks, str):
                _banks = [x.strip() for x in _banks.split(",") if x.strip()]
            if not _banks and isinstance(args.get("bank"), list):
                _banks = args.get("bank")
            if _banks:
                try:
                    by = _br.search_reviews_multi(_q, banks=_banks,
                                                  product=args.get("product"), k_per=max(_k // 2, 6))
                except Exception as e:
                    return json.dumps({"error": f"search_complaints multi failed: {e}"}, ensure_ascii=False)
                out = {b: [{"product": r.get("product"), "date": r.get("date"), "url": r.get("url"),
                            "text": (r.get("text") or "")[:600]} for r in revs]
                       for b, revs in by.items()}
                empties = [b for b, v in out.items() if not v]
                resp = {"mode": "per_bank", "by_bank": out,
                        "counts": {b: len(v) for b, v in out.items()}}
                if empties:
                    resp["empty_banks_note"] = ("Без жалоб по теме в корпусе: " + ", ".join(empties) +
                                                " (возможно вне корпуса banki.ru или нет данных).")
                return json.dumps(resp, ensure_ascii=False, default=str)
            try:
                res = _br.search_reviews(_q, bank=args.get("bank"),
                                          product=args.get("product"), k=_k)
            except Exception as e:
                return json.dumps({"error": f"search_complaints failed: {e}"}, ensure_ascii=False)
            if not res:
                if args.get("bank"):
                    note = ("По этому банку ничего не нашлось. Если задавал query — повтори "
                            "БЕЗ query (discovery по банку, темы проступят сами). Если и так "
                            "пусто — банк вне корпуса banki.ru (217 банков).")
                else:
                    note = ("Запрос без bank вернул пусто. Чтобы оценить конкретный банк — "
                            "передай bank=<банк>.")
                return json.dumps({"results": [], "count": 0, "note": note}, ensure_ascii=False)
            out = {"count": len(res), "results": [
                {"bank": r.get("bank"), "product": r.get("product"), "date": r.get("date"),
                 "url": r.get("url"), "text": (r.get("text") or "")[:700]} for r in res]}
            if not args.get("bank"):
                # бесбанковый запрос = общий рыночный top-k, НЕ покрывает все банки →
                # запрет делать вывод об отсутствии жалоб у конкретного банка
                out["note"] = ("ВНИМАНИЕ: запрос без bank — общий рыночный top-k по теме. Он "
                               "охватывает лишь ближайшие по смыслу жалобы и структурно НЕ "
                               "покрывает все 217 банков (банк может быть в корпусе, но не "
                               "попасть в top-k). НЕ делай вывод, что у банка нет жалоб. Для "
                               "КАЖДОГО интересующего банка вызови search_complaints отдельно "
                               "с bank=<банк>.")
            return json.dumps(out, ensure_ascii=False, default=str)

        if name == "get_bank_ratings":
            rows = s.execute(text("""
                SELECT b.name, b.is_sber,
                       t.rate_pct avg_grade,
                       (t.raw->>'total_reviews')::int total_reviews,
                       round((t.raw->>'solved_pct')::numeric,1) solved_pct,
                       (t.raw->>'place')::int place
                  FROM product_offer o JOIN bank b USING(bank_id)
                  JOIN product_terms t ON t.offer_id=o.offer_id AND t.valid_to IS NULL
                 WHERE o.category='other' AND t.rate_kind='avg_grade'
                   AND (t.raw->>'total_reviews')::int > 0
                 ORDER BY (t.raw->>'total_reviews')::int DESC
                 LIMIT :n
            """), {"n": args.get("top_n", 15)}).mappings().all()
            return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)

        if name == "get_change_history":
            slug = args.get("bank_slug", "all")
            bank_filter = "AND b.slug = :s" if slug and slug != "all" else ""
            rows = s.execute(text(f"""
                SELECT b.name bank_name, o.category, o.title,
                       ch.changed_at, ch.diff
                  FROM change_history ch
                  JOIN product_offer o USING(offer_id)
                  JOIN bank b USING(bank_id)
                 WHERE 1=1 {bank_filter}
                 ORDER BY ch.changed_at DESC LIMIT :l
            """), {**({"s": slug} if slug and slug != "all" else {}),
                   "l": args.get("limit", 20)}).mappings().all()
            return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)

    if name == "run_sql":
        return _run_sql_safe(args.get("sql", ""))

    if name == "get_review_themes":
        return _get_review_themes(args.get("bank_slug"), args.get("period", "all"))

    if name == "get_bank_features":
        return _get_bank_features(args.get("bank_slug"),
                                    args.get("feature_key"),
                                    args.get("year"))

    if name == "semantic_search":
        return _semantic_search(
            args.get("query", ""),
            bank_slugs=args.get("bank_slugs"),
            doc_types=args.get("doc_types"),
            trust_min=float(args.get("trust_min", 0.5)),
            top_k=int(args.get("top_k", 6)),
        )

    if name == "fetch_official":
        return _fetch_official(
            url=args.get("url"),
            bank_slug=args.get("bank_slug"),
            topic=args.get("topic"),
            query=args.get("query"),
            use_browser=bool(args.get("use_browser", False)),
        )

    return json.dumps({"error": "unknown tool"})


def _get_review_themes(bank_slug: str | None, period: str = "all") -> str:
    """Читает review_summary. Если для данного банка/периода нет — на лету
    генерит и записывает (на следующий запрос будет из кеша)."""
    if not bank_slug:
        return json.dumps({"error": "bank_slug обязателен"}, ensure_ascii=False)
    period = period or "all"
    if period not in ("all", "last_30d", "last_90d"):
        return json.dumps({"error": f"unsupported period: {period}"}, ensure_ascii=False)

    with db.session() as s:
        row = s.execute(text("""
            SELECT b.bank_id, b.name, b.is_sber FROM bank b WHERE b.slug = :s
        """), {"s": bank_slug}).first()
        if not row:
            return json.dumps({"error": f"банк {bank_slug} не найден"},
                              ensure_ascii=False)
        bank_id, bank_name, is_sber = row[0], row[1], row[2]

        summary = s.execute(text("""
            SELECT total_reviews, avg_rating,
                   sentiment_pos, sentiment_neg, sentiment_neu,
                   top_complaints, top_praise, by_source, generated_at
              FROM review_summary
             WHERE bank_id = :b AND period = :p
        """), {"b": bank_id, "p": period}).mappings().first()

    if not summary:
        # Lazy build на лету
        try:
            from ..rag.summarizer import rebuild_for_bank
            rebuild_for_bank(bank_id, period)
            with db.session() as s:
                summary = s.execute(text("""
                    SELECT total_reviews, avg_rating,
                           sentiment_pos, sentiment_neg, sentiment_neu,
                           top_complaints, top_praise, by_source, generated_at
                      FROM review_summary
                     WHERE bank_id = :b AND period = :p
                """), {"b": bank_id, "p": period}).mappings().first()
        except Exception as e:
            return json.dumps({"error": f"summary build failed: {e}"},
                              ensure_ascii=False)

    if not summary:
        return json.dumps({
            "bank_slug": bank_slug, "bank_name": bank_name,
            "period": period, "note": "Недостаточно отзывов (минимум 20)",
        }, ensure_ascii=False)

    return json.dumps({
        "bank_slug": bank_slug, "bank_name": bank_name, "is_sber": is_sber,
        "period": period,
        "total_reviews":  summary["total_reviews"],
        "avg_rating":     float(summary["avg_rating"]) if summary["avg_rating"] else None,
        "sentiment":      {"pos": summary["sentiment_pos"],
                           "neg": summary["sentiment_neg"],
                           "neu": summary["sentiment_neu"]},
        "top_complaints": summary["top_complaints"],
        "top_praise":     summary["top_praise"],
        "by_source":      summary["by_source"],
        "generated_at":   summary["generated_at"].isoformat() if summary["generated_at"] else None,
    }, ensure_ascii=False, default=str)


def _get_bank_features(bank_slug: str | None, feature_key: str | None = None,
                        year: int | None = None) -> str:
    if not bank_slug:
        return json.dumps({"error": "bank_slug обязателен"}, ensure_ascii=False)
    where = ["b.slug = :s"]
    params: dict = {"s": bank_slug}
    if feature_key:
        where.append("bf.feature_key LIKE :fk")
        params["fk"] = f"{feature_key}%"
    if year:
        where.append("bf.feature_key LIKE :yr")
        params["yr"] = f"%{year}%"
    sql = f"""
        SELECT bf.feature_key, bf.feature_value, bf.confidence,
               bf.source_url, bf.extracted_at
          FROM bank_feature bf
          JOIN bank b USING(bank_id)
         WHERE {' AND '.join(where)}
         ORDER BY bf.extracted_at DESC
         LIMIT 50
    """
    with db.session() as s:
        rows = s.execute(text(sql), params).mappings().all()
    out = []
    for r in rows:
        v = r["feature_value"] or {}
        out.append({
            "feature_key": r["feature_key"],
            "value": v.get("value") if isinstance(v, dict) else v,
            "unit": (v or {}).get("unit"),
            "currency": (v or {}).get("currency"),
            "year": (v or {}).get("year"),
            "claim_text": (v or {}).get("claim_text"),
            "confidence": float(r["confidence"]) if r["confidence"] else None,
            "source_url": r["source_url"],
        })
    return json.dumps({"bank_slug": bank_slug, "features": out, "count": len(out)},
                      ensure_ascii=False, default=str)


def _semantic_search(query: str, *, bank_slugs=None, doc_types=None,
                     trust_min: float = 0.5, top_k: int = 6) -> str:
    """Tool: pgvector search в document_chunk + format with citations."""
    if not query or not query.strip():
        return json.dumps({"error": "query пустой"}, ensure_ascii=False)
    try:
        from ..rag.retriever import semantic_search as _ss
        results = _ss(
            query, top_k=top_k,
            bank_slugs=bank_slugs, doc_types=doc_types,
            trust_min=trust_min, exclude_sponsored=True,
        )
    except Exception as e:
        return json.dumps({"error": f"semantic_search failed: {e}"},
                          ensure_ascii=False)

    out = []
    for r in results:
        out.append({
            "text":          r["text"][:1500],        # 1500: больше деталей в snippet — раньше 600 резало надбавки/условия
            "headings_path": r.get("headings_path"),
            "bank_slug":     r.get("bank_slug"),
            "bank_name":     r.get("bank_name"),
            "url":           r.get("url"),
            "doc_type":      r.get("doc_type"),
            "trust_score":   float(r.get("trust_score") or 0),
            "source_kind":   r.get("source_kind"),
            "fetched_at":    r["fetched_at"].isoformat() if r.get("fetched_at") else None,
            "relevance":     round(r.get("relevance", 0), 3),
        })
    return json.dumps({"query": query, "results": out, "count": len(out)},
                      ensure_ascii=False, default=str)


def _fetch_official(url: str | None = None,
                    bank_slug: str | None = None,
                    topic: str | None = None,
                    query: str | None = None,
                    use_browser: bool = False) -> str:
    """Tool: cold fetch + index + (optional) in-memory ranking.

    Сценарии:
      • url задан → fetch + index, возвращаем top chunks по query (если задан)
      • bank_slug + topic → берём URL из bank_profile.key_pages → fetch + index
      • bank_slug + query (без topic) → ищем релевантный URL по sitemap

    Side effect: новый document/chunks записываются в БД (passive enrichment).
    """
    target_url = url

    # Резолвим URL по bank_slug + topic
    if not target_url and bank_slug and topic:
        with db.session() as s:
            row = s.execute(text("""
                SELECT key_pages, official_url FROM bank_profile bp
                  JOIN bank b USING(bank_id)
                 WHERE b.slug = :s
            """), {"s": bank_slug}).first()
        if row:
            kp = row[0] or {}
            urls_for_topic = kp.get(topic) if isinstance(kp, dict) else None
            if urls_for_topic and isinstance(urls_for_topic, list) and urls_for_topic:
                target_url = urls_for_topic[0]
            else:
                target_url = row[1]    # fallback на homepage

    if not target_url:
        return json.dumps({
            "error": "не задан ни url, ни bank_slug+topic, ни bank_profile",
        }, ensure_ascii=False)

    # Ingest
    try:
        from ..rag.indexer import ingest_document_from_url
        result = ingest_document_from_url(
            target_url, bank_slug_hint=bank_slug,
            prefer_browser=use_browser,
        )
    except Exception as e:
        return json.dumps({"error": f"fetch_failed: {e}"}, ensure_ascii=False)

    response: dict = {
        "url":            result.url,
        "bank_slug":      bank_slug,
        "doc_type":       result.doc_type,
        "trust_score":    result.trust_score,
        "is_sponsored":   result.is_sponsored,
        "is_new":         result.is_new,
        "chunks_added":   result.chunks_added,
        "skipped_reason": result.skipped_reason,
    }

    # Если есть query — сразу делаем semantic_search в свежесвалявшем документе
    if query and result.document_id:
        with db.session() as s:
            rows = s.execute(text("""
                SELECT chunk_id, text, headings_path, idx
                  FROM document_chunk WHERE document_id = :d ORDER BY idx
                 LIMIT 50
            """), {"d": result.document_id}).mappings().all()
        if rows:
            from ..rag import embedder
            qvec = embedder.embed_one(query)
            scored = []
            for r in rows:
                # Re-embed chunks мы уже не делаем — они в БД, можно через SQL
                pass  # упростим: используем текстовый поиск как fallback
            # Простой текстовый поиск: keyword match в chunk.text
            ql = query.lower()
            keyword_rank = []
            for r in rows:
                t = r["text"].lower()
                hits = sum(1 for w in ql.split() if w in t)
                keyword_rank.append((hits, r))
            keyword_rank.sort(key=lambda x: x[0], reverse=True)
            top = [{"text": r["text"][:600], "headings_path": r["headings_path"],
                    "idx": r["idx"]} for hits, r in keyword_rank[:5] if hits > 0]
            response["top_relevant"] = top

    return json.dumps(response, ensure_ascii=False, default=str)


_KNOWN_TOOLS = {t["function"]["name"] for t in TOOLS}

def _extract_text_tool_call(text: str) -> dict | None:
    """Парсит tool call из текстового вывода модели (fallback для моделей
    без native function calling).

    Поддерживаемые форматы:
      1. {"type":"function","name":"...", "parameters": {...}}   ← Llama/Fireworks
      2. {"name":"...","arguments":{...}}                        ← generic
      3. <tool_call>{"name":"...","arguments":...}</tool_call>   ← Llama instruct
    """
    import re as _re

    def _find_json_objects(s: str) -> list[dict]:
        """Находит все top-level JSON-объекты в строке через подсчёт скобок."""
        results = []
        for start in range(len(s)):
            if s[start] != '{':
                continue
            depth, end = 0, -1
            for i in range(start, len(s)):
                if s[i] == '{':
                    depth += 1
                elif s[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end == -1:
                continue
            try:
                obj = json.loads(s[start:end])
                results.append(obj)
                break  # берём первый валидный
            except Exception:
                pass
        return results

    def _normalize_args(args: object) -> dict:
        """Нормализует аргументы из schema-формата Llama в плоский dict."""
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                return {}
        if not isinstance(args, dict):
            return {}
        # Llama format: {"type":"object","properties":{"cat":{"type":"string","value":"deposit"}}}
        if args.get("type") == "object" and "properties" in args:
            props = args["properties"]
            return {k: (v.get("value") if isinstance(v, dict) and "value" in v
                        else v.get("default") if isinstance(v, dict) else v)
                    for k, v in props.items()}
        return args

    # Формат 3: XML-тег <tool_call>...</tool_call>
    m = _re.search(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', text, _re.DOTALL)
    if m:
        for obj in _find_json_objects(m.group(1)):
            name = obj.get("name") or obj.get("function", {}).get("name", "")
            if name in _KNOWN_TOOLS:
                return {"name": name, "arguments": _normalize_args(obj.get("arguments") or obj.get("parameters") or {})}

    # Форматы 1 и 2: скан всех JSON-объектов
    for obj in _find_json_objects(text):
        # Форматы {name, arguments} / {name, parameters}
        name = obj.get("name") or obj.get("function", {}).get("name", "")
        if name in _KNOWN_TOOLS:
            return {"name": name, "arguments": _normalize_args(obj.get("arguments") or obj.get("parameters") or {})}

        # Формат 4 (Llama/Fireworks):
        # {"type":"function","parameters":{"type":"object","properties":{"function":"TOOL","parameters":{...}}}}
        if obj.get("type") == "function" and isinstance(obj.get("parameters"), dict):
            props = obj["parameters"].get("properties", {})
            name4 = props.get("function", "") or props.get("name", "")
            if isinstance(name4, str) and name4 in _KNOWN_TOOLS:
                return {"name": name4, "arguments": _normalize_args(props.get("parameters", {}))}

        # Формат 5: {"type":"function","function":{"name":"...","parameters":{...}}}
        if obj.get("type") == "function" and isinstance(obj.get("function"), dict):
            fn = obj["function"]
            name5 = fn.get("name", "")
            if isinstance(name5, str) and name5 in _KNOWN_TOOLS:
                return {"name": name5, "arguments": _normalize_args(fn.get("parameters") or fn.get("arguments") or {})}

    return None


def _extract_sources_from_tool_result(tool_name: str, result_json: str,
                                        sources: list[dict]) -> str:
    """Из результата tool'а вытаскивает источники, добавляет в общий список,
    возвращает результат с подставленными [N] маркерами для LLM.

    Поддерживаемые форматы:
      semantic_search: {"results": [{url, bank_name, headings_path, ...}]}
      fetch_official:  {"url", "top_relevant": [{...}]}

    sources mutates in-place. Возвращает enriched-result string для LLM.
    """
    try:
        data = json.loads(result_json)
    except Exception:
        return result_json
    if not isinstance(data, dict):
        return result_json

    # P1.7 source-dedup: считаем «эквивалентными» URL'ы которые отличаются
    # только query-параметрами (?type=all vs ?type=otz на banki.ru или
    # tracking-параметрами utm_*). Контент таких страниц перекрывается
    # >70%. Сливаем в один source чтобы не раздувать UI и не дезориентировать
    # synthesizer.
    from urllib.parse import urlparse, parse_qs
    def _canonical_url(u: str) -> str:
        try:
            p = urlparse(u)
            host = (p.hostname or "").replace("www.", "")
            path = p.path.rstrip("/")
            # banki.ru/services/responses/bank/X — query type=all/otz/etc
            # эквивалентны для нашей цели
            if "banki.ru/services/responses" in host + path:
                return f"{host}{path}"
            # Прочие — оставляем path; query отбрасываем кроме семантичных
            qs = parse_qs(p.query)
            keep = {k: v for k, v in qs.items()
                    if k.lower() not in {"utm_source","utm_medium","utm_campaign",
                                          "utm_term","utm_content","ref","type",
                                          "from","gclid","fbclid","yclid"}}
            if keep:
                qpart = "&".join(f"{k}={v[0]}" for k, v in sorted(keep.items()))
                return f"{host}{path}?{qpart}"
            return f"{host}{path}"
        except Exception:
            return u

    def _add_source(url, bank_name=None, headings_path=None,
                     trust_score=None, source_kind=None,
                     fetched_at=None, doc_type=None,
                     excerpt: str | None = None) -> int:
        """Возвращает порядковый номер [N] (1-based).
        Excerpts накапливаются для verifier'а: до 10 фрагментов × 800 chars."""
        canonical = _canonical_url(url)
        for i, s in enumerate(sources):
            # Дедуп: точное совпадение URL ИЛИ canonical (после стрипа query)
            if (s.get("url") == url or
                _canonical_url(s.get("url") or "") == canonical):
                if excerpt:
                    ex_list = s.setdefault("excerpts", [])
                    ex_clean = excerpt.strip()
                    if ex_clean and ex_clean not in ex_list and len(ex_list) < 10:
                        ex_list.append(ex_clean[:800])
                return i + 1
        sources.append({
            "n":             len(sources) + 1,
            "url":           url,
            "bank_name":     bank_name,
            "headings_path": headings_path,
            "trust_score":   trust_score,
            "source_kind":   source_kind,
            "fetched_at":    fetched_at,
            "doc_type":      doc_type,
            "excerpts":      [excerpt.strip()[:800]] if excerpt and excerpt.strip() else [],
        })
        return len(sources)

    # semantic_search formats
    if tool_name == "semantic_search" and isinstance(data.get("results"), list):
        enriched = []
        for r in data["results"]:
            n = _add_source(
                url=r.get("url"), bank_name=r.get("bank_name"),
                headings_path=r.get("headings_path"),
                trust_score=r.get("trust_score"),
                source_kind=r.get("source_kind"),
                fetched_at=r.get("fetched_at"),
                doc_type=r.get("doc_type"),
                excerpt=r.get("text"),
            )
            r2 = dict(r)
            r2["citation"] = f"[{n}]"
            enriched.append(r2)
        data["results"] = enriched
        return json.dumps(data, ensure_ascii=False, default=str)

    # fetch_official format
    if tool_name == "fetch_official" and data.get("url"):
        # Собираем excerpt из top_relevant если есть
        merged_excerpt = None
        tr = data.get("top_relevant")
        if isinstance(tr, list) and tr:
            merged_excerpt = " ".join(
                (it.get("text") or "")[:300] for it in tr[:2] if isinstance(it, dict)
            )[:600]
        n = _add_source(
            url=data["url"], bank_name=None,
            trust_score=data.get("trust_score"),
            doc_type=data.get("doc_type"),
            excerpt=merged_excerpt,
        )
        data["citation"] = f"[{n}]"
        return json.dumps(data, ensure_ascii=False, default=str)

    return result_json


async def _with_heartbeat(agen: AsyncIterator[str],
                            interval: float = 2.0) -> AsyncIterator[str]:
    """Прокидывает события из `agen`, но во время «тишины» (когда pipeline
    висит в длинном await — gather агентов, написание отчёта аналитиком,
    критик) периодически до-эмитит ПОСЛЕДНИЙ stage_status с обновлённым
    progress_elapsed. Без этого таймер в UI стоит на месте всю стадию, хотя
    работа идёт — жалоба «идёт / ~30s, а прошло уже 5 минут».

    Важно: НЕ отменяем __anext__ по таймауту — отмена async-генератора в
    середине await рвёт весь pipeline (CancelledError влетает внутрь).
    Держим один pending-таск и переиспользуем его между тиками; отменяем
    только при финальной очистке.
    """
    last_stage: dict | None = None
    stage_start = 0.0
    ait = agen.__aiter__()
    pending = asyncio.ensure_future(ait.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                # тишина дольше interval — тикаем таймер текущей стадии
                if last_stage is not None and (last_stage.get("estimate_s") or 0) > 0:
                    tick = dict(last_stage)
                    tick["progress_elapsed"] = int(time.monotonic() - stage_start)
                    yield json.dumps(tick, ensure_ascii=False)
                continue
            try:
                ev = pending.result()
            except StopAsyncIteration:
                return
            # сразу планируем следующий вызов — pending всегда «в работе»
            pending = asyncio.ensure_future(ait.__anext__())
            try:
                d = json.loads(ev)
                if isinstance(d, dict) and d.get("type") == "stage_status":
                    last_stage = d
                    stage_start = time.monotonic()
            except Exception:
                pass
            yield ev
    finally:
        if not pending.done():
            pending.cancel()
        try:
            await agen.aclose()
        except Exception:
            pass


async def stream_analysis(question: str, history: list[dict],
                           force_deep: bool | None = None) -> AsyncIterator[str]:
    """Главный entry-point AI-чата.
    Автоматически выбирает режим: quick (single-shot tool-use)
    либо deep (planner → multi-step → synthesize → verify → charts).
    force_deep — явный override (через UI toggle)."""
    # Routing: deep mode для исследовательских вопросов.
    # По умолчанию DEEP_V2=1 — агентская архитектура v2 (conductor → автономные
    # агенты → analyst → critic). При DEEP_V2=0 откат на старый EAV-pipeline.
    from .llm_utils import is_deep_question
    if force_deep is True or (force_deep is None and is_deep_question(question)):
        if os.getenv("DEEP_V2", "1").lower() in ("1", "true", "yes"):
            yielded = False          # §4d: анти «двойной стрим» — откат только если
            # НИЧЕГО ещё не стримили. Если v2 упала ПОСЛЕ части SSE-событий, откат на
            # EAV даст склееный двойной отчёт (mode/plan/отчёт ×2 в один поток) →
            # вместо этого честно завершаем с ошибкой.
            try:
                from ..research.v2.orchestrator import stream_deep_research_v2
                # _with_heartbeat тикает progress_elapsed во время длинных
                # await'ов pipeline — таймер в UI идёт, а не «висит на ~30s».
                async for ev in _with_heartbeat(
                        stream_deep_research_v2(question, history)):
                    yield ev
                    yielded = True
                return
            except Exception as e:
                log.warning("[stream_analysis] v2 failed (%s); yielded=%s",
                             e, yielded)
                if yielded:
                    # Уже отдали часть потока — НЕ перезапускаем EAV (склейка).
                    # Завершаем с явной ошибкой; фронт покажет что есть.
                    yield json.dumps({"type": "text",
                                       "chunk": "\n\n⚠ _Сбой на этапе синтеза "
                                                "отчёта. Частичный результат выше._\n"},
                                      ensure_ascii=False)
                    yield json.dumps({"type": "done"}, ensure_ascii=False)
                    return
                # Ничего не стримили — безопасно откатиться на EAV.
        # Legacy EAV-pipeline (fallback)
        from ..research.orchestrator import stream_eav_research
        async for ev in stream_eav_research(question, history):
            yield ev
        return

    # Quick path (текущий single-shot)
    yield json.dumps({"type": "mode", "value": "quick"})

    client = AsyncOpenAI(
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY,
        max_retries=4,         # default 2; повышаем т.к. Fireworks 5xx бывают
        timeout=120.0,         # safety cap на одну операцию
    )
    # Reasoning-модели (gpt-oss/glm/kimi/deepseek) тратят токены на CoT —
    # без reasoning_effort=low content отвечает обрезано или пусто.
    from .llm_utils import _patch_client_reasoning_effort
    client = _patch_client_reasoning_effort(client)

    messages = [
        {"role": "system", "content": today_anchor() + "\n\n" + SYSTEM},
        *history,
        {"role": "user", "content": question},
    ]

    # Citation tracker: накапливаем источники по ходу tool-calls
    sources: list[dict] = []

    max_iters = 6  # защита от бесконечного цикла
    # После text-based tool call убираем tools из следующего запроса,
    # чтобы Llama не зациклилась на вызовах вместо текстового ответа.
    force_text_response = False

    for _ in range(max_iters):
        response_text = ""
        pending_tool_calls: dict[int, dict] = {}  # index → {id, name, arguments}

        create_kwargs: dict = dict(
            model=LLM_MODEL_NAME,
            messages=messages,
            max_tokens=4096,
            stream=True,
        )
        if not force_text_response:
            create_kwargs["tools"] = TOOLS
            create_kwargs["tool_choice"] = "auto"

        stream = await client.chat.completions.create(**create_kwargs)

        finish_reason = None
        buffered_chunks: list[str] = []  # буфер пока не ясно — tool call или текст
        streaming_mode = force_text_response  # при force_text — стримим сразу

        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if choice is None:
                continue
            finish_reason = choice.finish_reason or finish_reason
            delta = choice.delta

            if delta.content:
                response_text += delta.content

                if streaming_mode:
                    # Уже решили стримить — отдаём сразу
                    yield json.dumps({'type': 'text', 'chunk': delta.content})
                else:
                    # Буферизуем пока смотрим на первый символ
                    buffered_chunks.append(delta.content)
                    stripped = response_text.lstrip()
                    # Если первый значимый символ НЕ '{' — это точно не JSON tool call
                    if stripped and not stripped.startswith('{'):
                        streaming_mode = True
                        # Сбрасываем накопленный буфер и дальше стримим
                        for ch in buffered_chunks:
                            yield json.dumps({'type': 'text', 'chunk': ch})
                        buffered_chunks = []

            # Накапливаем структурированные tool_calls
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in pending_tool_calls:
                        pending_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        pending_tool_calls[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            pending_tool_calls[idx]["name"] += tc.function.name
                        if tc.function.arguments:
                            pending_tool_calls[idx]["arguments"] += tc.function.arguments

        # ── Путь 1: нативные tool_calls ─────────────────────────────────────
        if finish_reason == "tool_calls" and pending_tool_calls:
            tool_calls_msg = [
                {
                    "id": tc["id"] or f"call_{i}",
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for i, tc in enumerate(pending_tool_calls.values())
            ]
            messages.append({"role": "assistant", "content": response_text or None,
                             "tool_calls": tool_calls_msg})

            for i, tc in enumerate(pending_tool_calls.values()):
                yield json.dumps({'type': 'tool_call', 'name': tc['name']})
                try:
                    args = json.loads(tc["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = _run_tool(tc["name"], args)
                # Citation tracking: enriches LLM-side data с [N] метками
                result = _extract_sources_from_tool_result(tc["name"], result, sources)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"] or f"call_{i}",
                    "content": result,
                })
            continue

        # ── Путь 2: text-based tool call (только если буферизовали) ────────────
        # Если streaming_mode=True — текст уже отдан, tool call невозможен.
        if not streaming_mode:
            log.info("analyst: finish_reason=%s text[:120]=%r", finish_reason, response_text[:120])
            parsed = _extract_text_tool_call(response_text)
            log.info("analyst: _extract → %s", parsed)
            if parsed:
                name, args = parsed["name"], parsed["arguments"]
                yield json.dumps({'type': 'tool_call', 'name': name})
                result = _run_tool(name, args)
                # Citation tracking
                result = _extract_sources_from_tool_result(name, result, sources)
                log.info("analyst: tool %s → %d chars", name, len(result))
                force_text_response = True
                messages.append({"role": "assistant", "content": response_text})
                messages.append({"role": "user", "content":
                    f"Данные получены. Вот результат:\n{result}\n\n"
                    f"Напиши ТОЛЬКО текстовый аналитический ответ на русском языке. "
                    f"Используй markdown-таблицы и заголовки. ОБЯЗАТЕЛЬНО ставь маркеры [1], [2] "
                    f"в местах где упоминаешь данные из источников (см. citation в результатах). "
                    f"Не вызывай функции."})
                continue

        # ── Завершено ────────────────────────────────────────────────────────────
        if streaming_mode:
            # Текст уже был отстримлен в реальном времени
            pass
        else:
            # Буфер содержит текст (который не распознан как tool call).
            # Убираем любой JSON-префикс на случай нераспознанного формата.
            json_buf, clean = "", []
            for ch in buffered_chunks:
                json_buf += ch
                stripped = json_buf.lstrip()
                if stripped.startswith("{"):
                    depth, end = 0, -1
                    for i, c in enumerate(json_buf):
                        depth += (c == "{") - (c == "}")
                        if depth == 0:
                            end = i + 1
                            break
                    if end > 0:
                        remainder = json_buf[end:]
                        try:
                            json.loads(json_buf[:end])  # валидный JSON → пропускаем
                        except Exception:
                            clean.append(json_buf[:end])
                        if remainder.strip():
                            clean.append(remainder)
                        json_buf = ""
                    # else: JSON не завершился — продолжаем накапливать
                else:
                    clean.append(json_buf)
                    json_buf = ""
            if json_buf:
                clean.append(json_buf)

            text_out = "".join(clean).strip()
            if not text_out:
                yield json.dumps({'type': 'text', 'chunk': '(пустой ответ — попробуйте переформулировать запрос)'})
            else:
                yield json.dumps({'type': 'text', 'chunk': text_out})

        # Стримим собранные источники до 'done', чтобы UI отрисовал sources panel
        if sources:
            yield json.dumps({'type': 'sources', 'sources': sources}, ensure_ascii=False)
        yield json.dumps({'type': 'done'})
        break

