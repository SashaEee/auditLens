"""Фоновые циклы дайджеста + ежедневный автосбор тарифов.

  digest_background_loop — генерация выпуска в DIGEST_GEN_HOUR_MSK (07:00) +
                           catch-up после рестарта контейнера
  ensure_digest          — идемпотентный запуск генерации (утро/lazy/manual);
                           stampede-защита: asyncio.Lock (процесс) +
                           pg advisory lock (межпроцессный, auto-release)
  ingest_background_loop — автосбор тарифов в INGEST_HOUR_MSK (05:00) + quality:
                           до этого сбор запускался только кнопкой → change_history
                           не наполнялся, и «Тарифные движения недели» были бы пусты
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta

from sqlalchemy import text

from .. import db
from ..clock import MSK
from . import pipeline, store

log = logging.getLogger(__name__)

GEN_HOUR = int(os.getenv("DIGEST_GEN_HOUR_MSK", "7"))
INGEST_HOUR = int(os.getenv("INGEST_HOUR_MSK", "5"))
INGEST_DAILY = os.getenv("INGEST_DAILY", "1") == "1"
# Сторож свежести: если последний УСПЕШНЫЙ сбор был давнее — догоняем сразу,
# в любой час. Без него простой VM в 05:00 означал сутки протухших данных
# (окно catch-up было жёстко [INGEST_HOUR, +4ч)).
INGEST_MAX_STALE_H = int(os.getenv("INGEST_MAX_STALE_H", "26"))
WATCHDOG_EVERY_S = int(os.getenv("INGEST_WATCHDOG_EVERY_S", "3600"))
# lazy-прогоны не чаще раза в N секунд: одна перманентно падающая секция иначе
# гоняла бы регенерацию по кругу (каждый GET с поллинга)
LAZY_COOLDOWN_S = int(os.getenv("DIGEST_LAZY_COOLDOWN_S", "600"))

_proc_lock = asyncio.Lock()
# отметка последнего тика сторожа: доказательство, что цикл РАБОТАЕТ
_watchdog_tick: datetime | None = None


def _today_msk() -> date:
    return datetime.now(MSK).date()


def lazy_allowed() -> bool:
    """Можно ли lazy-генерить СЕГОДНЯШНИЙ выпуск прямо сейчас.
    Ночью (до GEN_HOUR МСК) — нет: визит в 00:30 иначе генерил бы «утренний»
    выпуск до автосбора тарифов 05:00, а прогон 07:00 становился бы no-op;
    до утра честно показываем вчерашний («действует до утра»)."""
    return datetime.now(MSK).hour >= GEN_HOUR


async def ensure_digest(trigger: str, day: date | None = None, force: bool = False,
                        sections: list[str] | None = None) -> bool:
    """Идемпотентно: дайджест дня есть и не force → no-op.
    True = генерация реально выполнена этим вызовом."""
    day = day or _today_msk()
    if not force and await asyncio.to_thread(store.day_complete, day, pipeline.REQUIRED):
        return False
    if trigger == "lazy":
        if not lazy_allowed():
            return False
        age = await asyncio.to_thread(store.last_finished_age_s, day)
        if age is not None and age < LAZY_COOLDOWN_S:
            return False           # недавно уже пробовали — cooldown
    if _proc_lock.locked():        # в этом процессе уже генерится
        return False
    async with _proc_lock:
        def _locked_run() -> bool:
            with store.try_acquire_day_lock(day) as got:
                if not got:        # другой процесс/реплика уже генерит
                    return False
                if not force and store.day_complete(day, pipeline.REQUIRED):
                    return False   # перепроверка под локом
                store.mark_run(day, trigger)
                # отдельный event-loop в worker-потоке: генерация (LLM, fetch)
                # не блокирует основной цикл FastAPI
                asyncio.run(pipeline.run_daily(day, force=force, only=sections))
                return True
        return await asyncio.to_thread(_locked_run)


async def digest_background_loop():
    """Утренняя генерация + catch-up. Паттерн alerts_background_loop."""
    await asyncio.sleep(90)                      # не толкаться на старте
    try:                                         # рестарт после GEN_HOUR → догоняем
        if datetime.now(MSK).hour >= GEN_HOUR:
            ran = await ensure_digest("morning-catchup")
            if ran:
                log.info("digest catch-up: сгенерирован выпуск %s", _today_msk())
    except Exception as e:  # noqa: BLE001
        log.warning("digest catch-up failed: %s", e)
    while True:
        now = datetime.now(MSK)
        nxt = now.replace(hour=GEN_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            await ensure_digest("morning")
        except Exception as e:  # noqa: BLE001
            log.warning("digest morning run failed: %s", e)


# ── ежедневный автосбор тарифов ──────────────────────────────────────────────

# Единый мьютекс сбора: автосбор ↔ ручной запуск из UI. Два параллельных сбора
# = два Chromium на ОДНОМ browser-профиле (launch_persistent_context упадёт или
# профиль залочится). web.app._do_ingest* берёт этот же лок.
import threading
INGEST_MUTEX = threading.Lock()


def _ingest_ran_today() -> bool:
    """Был ли сегодня (МСК) ЗАВЕРШЁННЫЙ прогон сбора (или живой свежий).
    Убитый деплоем прогон (вечный running) не считаем — иначе полдня без сбора
    «засчитывается» как выполненный."""
    with db.session() as s:
        row = s.execute(text("""
            SELECT count(*) FROM extraction_run
             WHERE started_at >= (now() AT TIME ZONE 'Europe/Moscow')::date
                                  AT TIME ZONE 'Europe/Moscow'
               AND (finished_at IS NOT NULL
                    OR started_at > now() - interval '30 minutes')
        """)).scalar()
    return bool(row and int(row) > 0)


def last_ok_ingest_age_h() -> float | None:
    """Часы с последнего успешного сбора тарифов (None — не было никогда)."""
    with db.session() as s:
        v = s.execute(text("""
            SELECT extract(epoch FROM now() - max(finished_at)) / 3600.0
              FROM extraction_run
             WHERE status = 'ok' AND finished_at IS NOT NULL
               AND source LIKE 'sravni%'
        """)).scalar()
    return float(v) if v is not None else None


def ingest_schedule() -> dict:
    """Реальное расписание и свежесть — источник правды для UI (без хардкода)."""
    now = datetime.now(MSK)
    nxt = now.replace(hour=INGEST_HOUR, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    age = last_ok_ingest_age_h()
    tick_age = ((now - _watchdog_tick).total_seconds()
                if _watchdog_tick else None)
    return {
        "enabled": INGEST_DAILY,
        # сторож считается живым, если тикал не позже двух интервалов назад
        "watchdog_alive": bool(tick_age is not None
                               and tick_age < WATCHDOG_EVERY_S * 2 + 300),
        "watchdog_last_tick": _watchdog_tick.isoformat() if _watchdog_tick else None,
        "ingest_hour_msk": INGEST_HOUR,
        "digest_hour_msk": GEN_HOUR,
        "max_stale_h": INGEST_MAX_STALE_H,
        "next_run_msk": nxt.isoformat(),
        "last_ok_age_h": round(age, 1) if age is not None else None,
        "stale": bool(age is None or age > INGEST_MAX_STALE_H),
    }


def _captcha_pending() -> bool:
    """Решается капча (in-process флаг веб-слоя) → сбор не запускаем.
    Lazy-импорт: web.app сам импортирует scheduler (иначе цикл)."""
    try:
        from ..web import app as webapp
        return bool(getattr(webapp, "_CAPTCHA_LOCK", False))
    except Exception:  # noqa: BLE001
        return False


def _run_ingest_all() -> None:
    """Все источники последовательно + quality-чеки. Каждый источник пишет свой
    статус в extraction_run — упавший не валит остальных."""
    if _captcha_pending():
        log.info("daily ingest: пропуск — решается капча")
        return
    if not INGEST_MUTEX.acquire(blocking=False):
        log.info("daily ingest: пропуск — сбор уже идёт")
        return
    try:
        from ..config import load_sources
        from ..orchestrator.runner import ingest
        sources = list(load_sources().keys())
        log.info("daily ingest: старт, источники: %s", sources)
        for src in sources:
            try:
                ingest(src, None)
            except Exception as e:  # noqa: BLE001
                log.warning("daily ingest %s failed: %s", src, e)
        try:
            from ..quality.checks import run_quality
            res = run_quality()
            log.info("daily ingest: quality %s", res)
        except Exception as e:  # noqa: BLE001
            log.warning("daily quality failed: %s", e)
        try:    # ротационная проверка ссылок офферов (404 → url=NULL)
            from ..normalizer.offers import validate_offer_urls
            log.info("daily ingest: url-check %s", validate_offer_urls())
        except Exception as e:  # noqa: BLE001
            log.warning("url-check failed: %s", e)
        try:
            # Смысл условий («бесплатно ВСЕГДА» или «при остатке 2,5 млн»)
            # регекспом не берётся, а без него ранг карт вырожден: 124 банка
            # из 163 стоят на нуле. Порция в сутки: модуль сам выбирает, у кого
            # разбора нет или он устарел. Здесь, а НЕ внутри ingest: суточный
            # цикл зовёт ingest по разу на источник, и обогащение шло бы
            # шесть раз подряд.
            import os as _os
            if _os.getenv("ENRICH_ENABLED", "1") not in ("0", "false", "no"):
                from ..normalizer.enrich_llm import enrich
                log.info("daily ingest: обогащение %s",
                         enrich(limit=int(_os.getenv("ENRICH_LIMIT", "150"))))
        except Exception as e:  # noqa: BLE001 — обогащение не валит цикл
            log.warning("обогащение не выполнено: %s", e)
        try:    # протухание: пропавшие из выдачи офферы гаснут (is_active=false)
            from ..normalizer.offers import expire_stale_offers
            expire_stale_offers()
        except Exception as e:  # noqa: BLE001
            log.warning("expire failed: %s", e)
    finally:
        INGEST_MUTEX.release()


async def ingest_background_loop():
    """Автосбор тарифов раз в день в INGEST_HOUR (МСК, до генерации дайджеста):
    change_history наполняется сам, а не только по кнопке. INGEST_DAILY=0 — выкл."""
    if not INGEST_DAILY:
        log.info("daily ingest: выключен (INGEST_DAILY=0)")
        return
    await asyncio.sleep(120)
    log.info("daily ingest: расписание %02d:00 МСК, сторож свежести %d ч",
             INGEST_HOUR, INGEST_MAX_STALE_H)

    async def _maybe_run(reason: str) -> None:
        if await asyncio.to_thread(_ingest_ran_today):
            return
        log.info("daily ingest: старт (%s)", reason)
        await asyncio.to_thread(_run_ingest_all)

    global _watchdog_tick
    while True:
        try:
            now = datetime.now(MSK)
            _watchdog_tick = now
            nxt = now.replace(hour=INGEST_HOUR, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            # ежедневный слот наступил → собираем; иначе просыпаемся раз в час
            # и проверяем СВЕЖЕСТЬ: простой VM/деплой в час сбора больше не
            # означает сутки протухших данных
            wait = min((nxt - now).total_seconds(), WATCHDOG_EVERY_S)
            await asyncio.sleep(wait)

            now = datetime.now(MSK)
            if now >= nxt or (now.hour == INGEST_HOUR):
                await _maybe_run("плановый слот")
                continue
            age = await asyncio.to_thread(last_ok_ingest_age_h)
            if age is None or age > INGEST_MAX_STALE_H:
                await _maybe_run(f"данные устарели ({age and round(age)} ч)")
        except Exception as e:  # noqa: BLE001
            log.warning("daily ingest loop failed: %s", e)
            await asyncio.sleep(300)


# ── Сторож ключевой ставки ───────────────────────────────────────────────────
# Ставка меняется 8 раз в год, но именно в эти дни аудитор на неё и смотрит.
# 27.07.2026: ЦБ снизил 14.25 → 14.00 с понедельника, но в 07:00 SOAP ещё отдавал
# пятничное значение. Оно закэшировалось и вмёрзло в выпуск дня — на главной весь
# день висело «Ставка 14.25%» и посчитанный от неё спред «−0.75 пп» вместо
# верных «−0.50 пп». Перечитать было некому: выпуск собирается раз в сутки.
#
# Сторож раз в час сверяет ставку с ЦБ и, заметив новое значение, пересобирает
# ровно те секции, которые от неё зависят: tariff_moves (снимок ставки, только
# SQL) и headline (текст и спред, один быстрый вызов модели). Остальные секции
# не трогаем — в них ставки нет, а лишняя пересборка перетирает выпуск.
KEYRATE_EVERY_S = int(os.getenv("KEYRATE_WATCH_EVERY_S", "3600"))
KEYRATE_SECTIONS = ("tariff_moves", "headline")
_keyrate_tick: datetime | None = None


def _known_key_rate() -> tuple[str, float] | None:
    """Ставка, на которой построен текущий выпуск (снимок в tariff_moves)."""
    from sqlalchemy import text as _t
    try:
        with db.session() as s:
            row = s.execute(_t("""
                SELECT payload->'key_rate'->>'current',
                       payload->'key_rate'->>'as_of'
                  FROM daily_digest
                 WHERE digest_date = current_date AND section = 'tariff_moves'
            """)).first()
    except Exception:  # noqa: BLE001
        return None
    if not row or row[0] is None:
        return None
    try:
        return (row[1] or ""), float(row[0])
    except (TypeError, ValueError):
        return None


async def keyrate_background_loop():
    """Раз в час: не изменил ли ЦБ ставку. Изменил → пересобрать зависящие секции."""
    from .news import fetch_key_rate
    await asyncio.sleep(180)          # даём приложению подняться
    log.info("сторож ключевой ставки: раз в %d с", KEYRATE_EVERY_S)
    global _keyrate_tick
    while True:
        try:
            await asyncio.sleep(KEYRATE_EVERY_S)
            _keyrate_tick = datetime.now(MSK)
            live = await asyncio.to_thread(fetch_key_rate)
            if not live or live.get("current") is None:
                continue
            known = await asyncio.to_thread(_known_key_rate)
            if known is None:
                continue              # выпуска ещё нет — соберётся штатно
            known_as_of, known_rate = known
            if abs(float(live["current"]) - known_rate) < 0.001:
                continue              # ставка та же — ничего не делаем
            log.info("ЦБ изменил ставку: %s (на %s) → %s (на %s), пересобираю %s",
                     known_rate, known_as_of, live["current"], live.get("as_of"),
                     ", ".join(KEYRATE_SECTIONS))
            await ensure_digest("keyrate", force=True,
                                sections=list(KEYRATE_SECTIONS))
        except Exception as e:  # noqa: BLE001
            log.warning("сторож ключевой ставки: %s", e)
            await asyncio.sleep(300)


def keyrate_watch_status() -> dict:
    """Для витрины «Источники»: сторож жив и когда последний раз просыпался."""
    return {"every_s": KEYRATE_EVERY_S,
            "last_tick": _keyrate_tick.isoformat() if _keyrate_tick else None,
            "alive": _keyrate_tick is not None}


# ── Предгенерация «Для вас» (этап C персонализации) ──────────────────────────
# Первый заход дня стоил ~21 с синхронного LLM-вызова (замер 05.08.2026) —
# юзер смотрел на скелетон. Теперь после утреннего выпуска (и после любой
# интрадей-пересборки ядра — кэш инвалидируется по generated_at) страница
# собирается фоном для всех активных за 7 дней. build_foryou сам no-op'ится
# по валидному кэшу, поэтому часовой цикл не жжёт лишних вызовов.
FORYOU_PREGEN = os.getenv("FORYOU_PREGEN", "1") == "1"
FORYOU_PREGEN_EVERY_S = int(os.getenv("FORYOU_PREGEN_EVERY_S", "3600"))


def _active_usernames(days: int = 7) -> list[str]:
    with db.session() as s:
        rows = s.execute(text("""
            SELECT username FROM app_user
             WHERE last_seen_at > now() - make_interval(days => :d)
               AND coalesce(prefs->>'personal_digest', 'true') <> 'false'
             ORDER BY last_seen_at DESC LIMIT 40
        """), {"d": days}).all()
    return [r[0] for r in rows]


async def foryou_pregen_loop():
    if not FORYOU_PREGEN:
        log.info("предгенерация «Для вас»: выключена (FORYOU_PREGEN=0)")
        return
    from . import personal, pipeline as _pipe
    await asyncio.sleep(420)          # после старта — дать ядру собраться первым
    log.info("предгенерация «Для вас»: активные за 7 дн, тик раз в %d с",
             FORYOU_PREGEN_EVERY_S)
    while True:
        try:
            now = datetime.now(MSK)
            if now.hour >= GEN_HOUR and await asyncio.to_thread(
                    store.day_complete, _today_msk(), _pipe.REQUIRED):
                users = await asyncio.to_thread(_active_usernames)
                built = 0
                for u in users:
                    try:
                        t0 = datetime.now(MSK)
                        await asyncio.wait_for(personal.build_foryou(u), timeout=90)
                        if (datetime.now(MSK) - t0).total_seconds() > 3:
                            built += 1          # реально собирали (не кэш-хит)
                    except Exception as e:  # noqa: BLE001
                        log.warning("предгенерация «Для вас» %s: %s", u, e)
                    await asyncio.sleep(2)      # не долбить LLM очередью
                if built:
                    log.info("предгенерация «Для вас»: собрано %d из %d активных",
                             built, len(users))
        except Exception as e:  # noqa: BLE001
            log.warning("предгенерация «Для вас»: %s", e)
        await asyncio.sleep(FORYOU_PREGEN_EVERY_S)


# ── Ночной судья новостного выпуска (этап 6) ─────────────────────────────────
# Через час после утренней генерации оценивает опубликованное строгой рубрикой →
# digest_news_judge → график «мусорной доли» в Пульсе. Деградацию отбора ловит
# метрика, а не глаз владельца (до аудита 05.08.2026 её не существовало вовсе).
JUDGE_HOUR = int(os.getenv("DIGEST_JUDGE_HOUR_MSK", str(GEN_HOUR + 1)))


async def judge_background_loop():
    from . import judge as judge_mod
    await asyncio.sleep(240)
    log.info("судья выпуска: расписание %02d:00 МСК", JUDGE_HOUR)
    while True:
        try:
            now = datetime.now(MSK)
            if now.hour >= JUDGE_HOUR:      # catch-up: сегодня ещё не оценён
                r = await judge_mod.judge_issue(_today_msk())
                if r.get("n"):
                    log.info("судья выпуска: %s", r)
            nxt = now.replace(hour=JUDGE_HOUR, minute=5, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            await asyncio.sleep((nxt - datetime.now(MSK)).total_seconds())
        except Exception as e:  # noqa: BLE001
            log.warning("судья выпуска: %s", e)
            await asyncio.sleep(1800)


# Корпус отзывов наполняет чужой крон, и мы не знаем его расписания, поэтому
# догоняем зеркало часто и небольшими порциями, а не раз в сутки: инкремент по
# водяному знаку почти бесплатен, когда догонять нечего.
FTS_SYNC_EVERY_S = int(os.getenv("BANKIRU_FTS_SYNC_EVERY_S", "3600"))


async def bankiru_fts_background_loop():
    """Держит полнотекстовое зеркало корпуса отзывов в актуальном состоянии.

    Без него словесная нога поиска отстаёт от источника ровно настолько, сколько
    приложение не перезапускали, и свежие жалобы находятся только по смыслу.
    Первый прогон после пустой миграции делает полный бэкфилл (замер на проде —
    169 тыс. уникальных отзывов за 151 с), поэтому стартует не сразу.
    """
    from ..rag import bankiru_fts
    await asyncio.sleep(90)           # даём приложению подняться
    log.info("зеркало отзывов: синхронизация раз в %d с", FTS_SYNC_EVERY_S)
    while True:
        try:
            r = await asyncio.to_thread(bankiru_fts.sync)
            if r.get("written"):
                log.info("индекс отзывов: из внешнего корпуса %d за %s с",
                         r["written"], r.get("seconds"))
        except Exception as e:  # noqa: BLE001
            log.warning("индекс отзывов, внешний корпус: %s", e)
        try:
            # Отзывы наших коллекторов: в индекс и со своими векторами. Идёт
            # ПОСЛЕ ночного сбора — тот пишет в таблицу review, а здесь
            # собранное становится видимым на вкладке и находимым поиском.
            r = await asyncio.to_thread(bankiru_fts.sync_local)
            if r.get("rows"):
                log.info("индекс отзывов: своих коллекторов %d, новых векторов %d за %s с",
                         r["rows"], r.get("embedded"), r.get("seconds"))
        except Exception as e:  # noqa: BLE001
            log.warning("индекс отзывов, свои коллекторы: %s", e)
        try:
            # Инкрементальная разметка тем: без неё свежая неделя оставалась
            # без меток до ручного assign (замер 05.08: 36 процентов окна),
            # и сигналы главной на разметке было не построить.
            from ..rag import review_topics
            await asyncio.to_thread(review_topics.label_new)
        except Exception as e:  # noqa: BLE001
            log.warning("инкрементальная разметка тем: %s", e)
        await asyncio.sleep(FTS_SYNC_EVERY_S)
