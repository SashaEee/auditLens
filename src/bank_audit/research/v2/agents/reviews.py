"""Reviews Agent — отзывы/жалобы/похвалы клиентов.

Стратегия web-first (БД неполная):
  1. Сначала проверяем что уже есть в БД (review_summary, run_sql по review).
  2. ОБЯЗАТЕЛЬНО лезем в web: site:banki.ru/products/otzyvy, irecommend,
     otzovik, vc.ru — свежие отзывы часто только там.
  3. Найденные отзывы пассивно индексируем в БД (index_review_passive).
  4. Кластеризуем жалобы по темам + sentiment + цитаты.

Финальный ответ — JSON с complaints[], sentiment_profile, coverage_notes
(включая «свежих жалоб мало — пробел сбора»).
"""
from __future__ import annotations

import logging

from ..base_agent import BaseAgent
from ..knowledge_bundle import Complaint, SentimentProfile, CoverageNote
from ..tools.tool_specs import REVIEWS_TOOLS

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — reviews-агент для аудиторской платформы. Собираешь
отзывы, жалобы и похвалы клиентов по заданной теме.

СТРАТЕГИЯ (DB-first — основной источник жалоб уже готов):
  1. search_reviews_db — ПЕРВЫМ И ОСНОВНЫМ (корпус banki.ru: ~390 тыс. жалоб
     1-2★ 2025-2026, 217 банков, с датами/ссылками; каждый отзыв = источник [N]):
     • Аудит по КОНКРЕТНОМУ продукту/теме (смотри ФОКУС/задание: эквайринг, ипотека,
       вклад, мобильное приложение, переводы…): передай bank + query=НАЗВАНИЕ этого
       продукта/темы (напр. query="эквайринг", "ипотека"). Это НЕ угадывание
       проблемы — реальные проблемы клиентов найдёт сама семантика по отзывам.
     • Общий обзор жалоб по банку: передай ТОЛЬКО bank, БЕЗ query (discovery —
       свежие жалобы, сам увидишь темы).
     ГЛАВНОЕ: НЕ выдумывай конкретную проблему в query — query это либо продукт/
     тема аудита, либо ничего. На каждый банк — 1-2 вызова.
     ⚠⚠ ВСЕГДА передавай bank. query БЕЗ bank — это общий рыночный top-k: он
     структурно НЕ покрывает все 217 банков (банк может быть в корпусе, но не
     попасть в top-k) и НЕ доказывает отсутствие жалоб. Для СРАВНЕНИЯ нескольких
     банков перечисли банки сам и вызови search_reviews_db ПО КАЖДОМУ отдельно
     (bank="Сбербанк", потом bank="ВТБ", потом bank="Альфа-Банк" …). НИКОГДА не
     пиши «у банка X жалоб нет», если не вызвал инструмент именно с bank=X.
  2. ТОЛЬКО если search_reviews_db вернул results=[] (банк ВНЕ корпуса) — тогда
     web_search ("{банк} {тема} отзыв жалоба" site:banki.ru/services/responses;
     при нужде irecommend/otzovik) + read_url 1-2 самых релевантных страниц.
     Не лезь в web, если жалобы уже нашлись в search_reviews_db.
  3. (опц.) run_sql по таблице review — если надо сверить с локальной агрегацией.

ВАЖНО про корпус: search_reviews_db содержит ТОЛЬКО негатив (1-2★) — это
жалобы. Похвал (praise) там НЕТ; не выдумывай позитив, оставь praise пустым,
если он не пришёл из web. Это нормально для аудита (нас интересуют риски).

ИЗВЛЕЧЕНИЕ ЖАЛОБ:
  • Группируй по ТЕМАМ (не «отзыв №1, отзыв №2», а «несработавший автоплатёж»,
    «скрытая комиссия», «сложность отмены», «СМС-спам»).
  • Каждая тема — с количеством отзывов (n_reviews), 2-3 дословными цитатами,
    датами и source_n из search_reviews_db.
  • Учитывай ДАТЫ: свежие 2025-2026 — приоритет, это текущие риски;
    что-то старше 2 лет — is_stale=true.

ВЕРИФИКАЦИЯ (важно!):
  • Если утверждение из отзыва спорное (1 голос против многих) — отметь в notes.
  • Не выдавай единичные жалобы за системные проблемы.

ВЫХОД (строгий JSON, БЕЗ markdown):
{
  "complaints": [
    {"subject":"Сбербанк","theme":"несработавший автоплатёж",
     "n_reviews":12,"sentiment":"neg",
     "sample_quotes":["«Настроил автоперевод, 2 месяца шёл, потом молча отвалился»", ...],
     "period":"2024-2025","source_ns":[5,7],"rating_avg":1.8,"is_stale":false}
  ],
  "praise": [
    {"subject":"Т-Банк","theme":"удобная настройка в приложении",
     "n_reviews":8,"sample_quotes":[...],"source_ns":[9]}
  ],
  "sentiment_profiles": [
    {"subject":"Сбербанк","total":325,"pos":0.31,"neu":0.20,"neg":0.49,
     "avg_rating":2.8,"source_ns":[2]}
  ],
  "index_reviews": [
    {"source":"banki_reviews","source_url":"https://...",
     "bank_name_raw":"Сбербанк","text":"полный текст отзыва",
     "rating":1,"title":"жалоба","posted_at":"2024-08-15"}
  ],
  "gaps": [
    {"subjects":["Газпромбанк"],
     "what":"свежие жалобы 2024-2026 не найдены",
     "reason":"отзывы по теме отсутствуют в открытых источниках",
     "recommendation":"парсить отзовики за 12 мес"}
  ],
  "summary":"Собрано N жалоб по M темам. Главный риск: ..."
}

Готов вернуть когда покрыты все объекты (или указаны пробелы) и сделано ≥3 tool-вызова.
"""


class ReviewsAgent(BaseAgent):
    SYSTEM_PROMPT = SYSTEM_PROMPT
    TOOLS = REVIEWS_TOOLS
    # Кластеризация цитат/жалоб — паттерн-матчинг, не числа; аналитик (Sonnet)
    # переформулирует → быстрая модель целиком.
    MODEL_TIER = "fast"

    async def _integrate(self, artifacts: dict) -> None:
        # complaints
        for c in (artifacts.get("complaints") or []):
            if not isinstance(c, dict):
                continue
            subject = str(c.get("subject") or "").strip()
            theme = str(c.get("theme") or "").strip()
            if not subject or not theme:
                continue
            try:
                n_rev = int(c.get("n_reviews") or 1)
            except (TypeError, ValueError):
                n_rev = 1
            self.bundle.add_complaint(Complaint(
                subject=subject,
                theme=theme,
                n_reviews=n_rev,
                sentiment=str(c.get("sentiment") or "neg"),
                sample_quotes=[str(q)[:300] for q in (c.get("sample_quotes") or [])][:5],
                period=str(c.get("period") or ""),
                source_ns=[int(n) for n in (c.get("source_ns") or [])
                            if str(n).isdigit()][:6],
                rating_avg=_safe_float(c.get("rating_avg")),
                is_stale=bool(c.get("is_stale")),
            ))

        # praise → тоже как complaints но sentiment=pos (для полноты картины)
        for p in (artifacts.get("praise") or []):
            if not isinstance(p, dict):
                continue
            subject = str(p.get("subject") or "").strip()
            theme = str(p.get("theme") or "").strip()
            if not subject or not theme:
                continue
            self.bundle.add_complaint(Complaint(
                subject=subject,
                theme=f"похвала: {theme}",
                n_reviews=int(p.get("n_reviews") or 1),
                sentiment="pos",
                sample_quotes=[str(q)[:300] for q in (p.get("sample_quotes") or [])][:5],
                source_ns=[int(n) for n in (p.get("source_ns") or [])
                            if str(n).isdigit()][:6],
                is_stale=False,
            ))

        # sentiment profiles
        for sp in (artifacts.get("sentiment_profiles") or []):
            if not isinstance(sp, dict):
                continue
            subject = str(sp.get("subject") or "").strip()
            if not subject:
                continue
            # Канонизируем «Сбербанк»→sberbank, иначе профиль не привяжется к
            # субъекту и не покажется в разборе.
            subject = self.bundle.canonical_subject(subject)
            self.bundle.sentiments.append(SentimentProfile(
                subject=subject,
                total=int(sp.get("total") or 0),
                pos=_safe_float(sp.get("pos")),
                neu=_safe_float(sp.get("neu")),
                neg=_safe_float(sp.get("neg")),
                avg_rating=_safe_float(sp.get("avg_rating")),
                source_ns=[int(n) for n in (sp.get("source_ns") or [])
                            if str(n).isdigit()][:6],
            ))

        # пассивная индексация отзывов в БД
        from ..passive_indexer import index_review_passive
        for r in (artifacts.get("index_reviews") or []):
            if not isinstance(r, dict):
                continue
            text = str(r.get("text") or "")
            if len(text) < 40:
                continue
            try:
                from datetime import datetime
                posted = None
                pa = r.get("posted_at")
                if pa:
                    try:
                        posted = datetime.fromisoformat(str(pa))
                    except Exception:
                        pass
                index_review_passive(
                    source=str(r.get("source") or "web_review"),
                    source_review_id=str(r.get("source_url", ""))[-60:],
                    source_url=str(r.get("source_url") or ""),
                    bank_name_raw=str(r.get("bank_name_raw") or ""),
                    text=text,
                    rating=_safe_float(r.get("rating")),
                    title=str(r.get("title") or "") or None,
                    posted_at=posted,
                )
            except Exception as e:
                log.info("passive review index failed: %s", e)

        # gaps
        for g in (artifacts.get("gaps") or []):
            if not isinstance(g, dict):
                continue
            what = str(g.get("what") or "").strip()
            if not what:
                continue
            self.bundle.coverage_notes.append(CoverageNote(
                what=what,
                subjects=[str(s) for s in (g.get("subjects") or [])],
                reason=str(g.get("reason") or "не найдено"),
                recommendation=str(g.get("recommendation") or ""),
            ))


def _safe_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
