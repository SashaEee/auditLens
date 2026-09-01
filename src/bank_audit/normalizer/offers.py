"""Нормализация черновиков офферов в нормализованную модель + SCD2 + change_history.
   Работает идемпотентно: повторный запуск без изменений данных не создаёт новых строк."""
from __future__ import annotations
import json
import re
from decimal import Decimal
from typing import Iterable
from sqlalchemy import text
from rapidfuzz import process, fuzz
import logging
from .. import db
from ..hashing import stable_digest

log = logging.getLogger(__name__)
from ..models import OfferDraft
from .rules import BANK_ALIASES, SBER_SLUGS, normalize_bank_key

NORMALIZE_FIELDS = (
    "rate_pct", "rate_kind", "currency",
    "amount_min", "amount_max", "term_months_min", "term_months_max",
    "fee_open", "fee_service", "grace_days", "cashback_pct",
    "early_withdraw", "capitalization", "replenishable",
    "conditions",
)

def _fuzzy_ok(key: str, alias: str) -> bool:
    """Вето на ложные fuzzy-склейки: WRatio даёт ~90 коротким ключам-подстрокам
    («ик банк» ⊂ «норвик банк», «сбер» ⊂ «сбережений») — так Тинькофф «всасывал»
    Металлинвестбанк, а Сбер — Национальный банк сбережений (аудит 22.07.2026:
    5 банков-магнитов, 24 чужих оффера). Принимаем матч, только если токены
    одной стороны — подмножество другой («сбербанк россии» ~ «сбербанк») или
    имена похожи целиком (опечатки: «сити банк» ~ «ситибанк»)."""
    kt, at = set(key.split()), set(alias.split())
    return kt <= at or at <= kt or fuzz.ratio(key, alias) >= 85


def bank_slug_for(session, raw_name: str) -> str:
    """Какой slug получит это написание имени — БЕЗ создания строки.

    Вынесено из resolve_bank, чтобы слияние дублей могло спросить «куда будет
    писать следующий сбор» и оставить именно ту строку. Иначе слитый дубль
    воскресает на следующий день под тем же именем.
    """
    raw_name = (raw_name or "").strip()
    key = normalize_bank_key(raw_name)
    slug = BANK_ALIASES.get(key)
    if not slug and key:
        # fuzzy: топ-5 кандидатов, а не единственный лучший — иначе короткий
        # ключ-подстрока («сбер») с тем же score перекрывает валидный «сбербанк»,
        # вето его режет, и «Сбербанк России» падал бы в unknown_
        for alias, score, _ in process.extract(
                key, list(BANK_ALIASES.keys()), scorer=fuzz.WRatio, limit=5):
            if score < 88:
                break
            if _fuzzy_ok(key, alias):
                slug = BANK_ALIASES[alias]
                break
    if not slug and key and re.fullmatch(r"[a-z0-9_-]+", key):
        # Источники иногда отдают латинский slug вместо имени («gazprombank»,
        # «psb») — до unknown_-фолбэка пробуем прямое совпадение со слагом уже
        # известного банка. Иначе плодятся латинские двойники (фидбек аналитиков;
        # 105 офферов были слиты миграцией 22.07.2026).
        row = session.execute(text("SELECT slug FROM bank WHERE slug=:s"),
                              {"s": key}).first()
        if row:
            return row[0]
    if not slug and key:
        # Написания, которые нормализатор не сводит к одному ключу («Банк
        # Оренбург» → «оренбург», «БАНКОРЕНБУРГ» → «банкоренбург»), сводит
        # справочник: слияние дублей складывает прежние написания в bank.aliases.
        # Без этой проверки колонка была мёртвой, а слитый дубль воскресал.
        row = session.execute(text("""
            SELECT slug FROM bank
             WHERE EXISTS (SELECT 1 FROM unnest(aliases) a
                            WHERE lower(a) = lower(:raw))
             LIMIT 1
        """), {"raw": (raw_name or "").strip()}).first()
        if row:
            return row[0]
    if not slug and key:
        # ПОСЛЕДНЯЯ попытка до placeholder: сопоставить с УЖЕ ЗАВЕДЁННЫМ банком
        # по имени, очищенному до букв и цифр. Рукописный словарь знает 63
        # банка, а собираем мы 800+, поэтому весь длинный хвост рынка попадал
        # в unknown_, и каждое новое написание («СОЛИД БАНК» против «Солид
        # Банк») заводило ЕЩЁ ОДИН банк: 55 групп дублей на проде, один банк
        # двумя строками в витрине и в ранге. Справочник — источник правды о
        # том, кого мы уже знаем; словарь остаётся только для алиасов.
        row = session.execute(text("""
            SELECT slug FROM bank
             WHERE lower(regexp_replace(name, '[^[:alnum:]]', '', 'g'))
                 = lower(regexp_replace(:raw, '[^[:alnum:]]', '', 'g'))
             ORDER BY (slug NOT LIKE 'unknown_%') DESC
             LIMIT 1
        """), {"raw": raw_name}).first()
        if row:
            return row[0]
    if not slug:
        # Пустое имя или "?" → placeholder-банк
        slug = "unknown_" + stable_digest({"n": key if key else "_empty_"})[:10]
    return slug


def resolve_bank(session, raw_name: str) -> int:
    """Резолвит raw-имя банка в bank_id (создаёт строку при необходимости)."""
    raw_name = (raw_name or "").strip()
    slug = bank_slug_for(session, raw_name)
    row = session.execute(text("SELECT bank_id FROM bank WHERE slug=:s"), {"s": slug}).first()
    if row:
        return row[0]
    return session.execute(text("""
        INSERT INTO bank(slug, name, is_sber)
        VALUES (:s, :n, :is_sber)
        RETURNING bank_id
    """), {"s": slug, "n": raw_name or "?", "is_sber": slug in SBER_SLUGS}).scalar_one()

def _digest(d: OfferDraft) -> str:
    payload = {f: getattr(d, f) for f in NORMALIZE_FIELDS}
    # см. OfferDraft.digest_extra: поля из raw, изменение которых обязано
    # создавать новую версию (иначе витрина показывает вечно старые числа)
    extra = getattr(d, "digest_extra", None)
    if extra:
        payload["_extra"] = extra
    return stable_digest(payload)

# ── сторож правдоподобия (аудит вкладки «Рынок» 11.08.2026) ──────────────────
# Витрина показала «Сбер #1 из 141 по вкладам, ставка 40 проц.» — промо-максимум,
# который парсер принял за ставку вклада; ровно 40.00 стояло у 18 банков сразу.
# Числа с такими признаками больше не попадают в сравнение молча: оффер
# сохраняется (история нужна), но помечается флагом качества и выключается из
# ранга, пока человек не подтвердит. Коридор привязан к ключевой ставке ЦБ —
# единственному эталону, который у нас есть ежедневно.
_GUARD_DEPOSIT_OVER_KEY = 5.0     # вклад выше ключевой на столько пп — подозрительно
_GUARD_CREDIT_UNDER_KEY = 3.0     # кредит ниже ключевой на столько пп — субсидия/тизер
_GUARD_GRACE_MAX_DAYS = 200       # больше — это рассрочка, а не грейс
_GUARD_FEE_MAX = 60000            # обслуживание дороже — проверить руками


def _key_rate() -> float | None:
    try:
        from ..digest.news import key_rate_from_db
        kr = key_rate_from_db(3) or {}
        return float(kr.get("current")) if kr.get("current") is not None else None
    except Exception:  # noqa: BLE001
        return None


def implausible(d: OfferDraft, key_rate: float | None) -> str | None:
    """Причина, по которой числу нельзя верить, либо None."""
    r = float(d.rate_pct) if d.rate_pct is not None else None
    if r is not None and key_rate:
        if d.category in ("deposit", "savings_account") and r > key_rate + _GUARD_DEPOSIT_OVER_KEY:
            return f"ставка вклада {r} при ключевой {key_rate}"
        if d.category in ("credit", "mortgage", "auto_loan") and r < key_rate - _GUARD_CREDIT_UNDER_KEY:
            # у ипотеки это чаще всего господдержка — она отсекается отдельно,
            # но пометить стоит: в ранг такие ставки идти не должны
            return f"ставка кредита {r} при ключевой {key_rate}"
    if d.grace_days and int(d.grace_days) > _GUARD_GRACE_MAX_DAYS:
        return f"грейс {d.grace_days} дн — вероятно рассрочка"
    if d.fee_service and float(d.fee_service) > _GUARD_FEE_MAX:
        return f"обслуживание {d.fee_service} руб/год"
    return None


def _flag_quality(session, offer_id: int, code: str, detail: str) -> None:
    try:
        session.execute(text("""
            INSERT INTO quality_flag(entity_type, entity_id, severity, code, detail)
            VALUES ('offer', :e, 'warn', :c, CAST(:d AS jsonb))
        """), {"e": offer_id, "c": code,
               "d": json.dumps({"reason": detail}, ensure_ascii=False)})
    except Exception:  # noqa: BLE001 — флаг не должен ломать нормализацию
        log.debug("quality_flag не записан", exc_info=True)


_SAVINGS_RE = re.compile(r"накопительн|сберегательн\s+сч[её]т|\bнакопит\b", re.I)


def _fix_category(d: OfferDraft) -> None:
    """Накопительный счёт — не срочный вклад: ставка плавающая, срока нет,
    условия начисления другие.

    Тип продукта берём из данных источника (depositType='accumulative'), а не
    из слова в названии: по названию 42 настоящих накопительных счёта 28 банков
    (включая два сберовских и лидера рынка МТС) оставались во «Вкладах», а
    12 срочных вкладов с «накопительным» в имени уезжали в накопительные
    (аудит 11.08.2026). Название — только запасной признак."""
    if d.category != "deposit":
        return
    dtype = str((d.raw or {}).get("deposit_type") or "").lower()
    if dtype in ("accumulative", "saving", "savings"):
        d.category = "savings_account"
        return
    if dtype in ("classic", "grow", "deal", "term", "urgent"):
        return                      # источник прямо говорит: это срочный вклад
    if _SAVINGS_RE.search(d.title or ""):
        d.category = "savings_account"


def upsert_offer(session, d: OfferDraft, snapshot_id: int | None,
                 source_page_id: int | None,
                 source_name: str = "sravni_aggregator") -> tuple[int, bool]:
    """source_name — КТО принёс оффер. Раньше здесь стояла константа
    «sravni_aggregator», и все 530 предложений с banki.ru подписывались чужим
    именем: аудитор видел ссылку на banki.ru и подпись «источник sravni»,
    а происхождение числа доказать было нечем (аудит 11.08.2026)."""
    _fix_category(d)
    bank_id = resolve_bank(session, d.bank_name_raw)
    doubt = implausible(d, _key_rate())
    row = session.execute(text("""
        INSERT INTO product_offer(bank_id, category, external_id, primary_source, title, url)
        VALUES (:b,:c,:e,:s,:t,:u)
        ON CONFLICT (bank_id, category, external_id) DO UPDATE
          SET last_seen=now(), title=EXCLUDED.title,
              url=COALESCE(EXCLUDED.url, product_offer.url),
              is_active=true    -- вернувшийся из протухания оффер оживает
        RETURNING offer_id
    """), {"b": bank_id, "c": d.category, "e": d.external_id,
           "s": source_name, "t": d.title, "u": d.url}).scalar_one()
    # сегмент клиента и вид продукта — ранг считается ВНУТРИ них, иначе
    # премиальная карта сравнивается с детской, а залоговый кредит с наличными
    from ..categories import classify_segment, classify_sub_segment
    sub = classify_sub_segment(d.category, d.title)
    if d.category == "rko" and not sub:
        # Форма бизнеса в названии тарифа обычно не написана («Модуль РКО ВВВ»),
        # а цена от неё зависит вдвое: у Сбера тот же пакет стоит 2 870 руб. ИП
        # и 5 170 руб. ООО. Форму отдаёт сам источник — берём её оттуда.
        types = [str(x).lower() for x in ((d.raw or {}).get("org_types") or [])]
        if types:
            sub = "any" if ("ip" in types and "ooo" in types) else (
                "ip" if "ip" in types else ("ooo" if "ooo" in types else None))
    session.execute(text("""
        UPDATE product_offer SET segment = :seg, sub_segment = :sub
         WHERE offer_id = :o
    """), {"seg": classify_segment(d.title), "sub": sub, "o": row})
    offer_id = row

    new_digest = _digest(d)
    cur = session.execute(text("""
        SELECT terms_id, digest FROM product_terms
         WHERE offer_id=:o AND valid_to IS NULL
         ORDER BY valid_from DESC LIMIT 1
    """), {"o": offer_id}).first()

    if doubt:
        _flag_quality(session, offer_id, "implausible_value", doubt)

    if cur and cur[1] == new_digest:
        return offer_id, False  # без изменений

    # закрываем текущую версию
    if cur:
        session.execute(text("UPDATE product_terms SET valid_to=now() WHERE terms_id=:t"),
                        {"t": cur[0]})

    new_id = session.execute(text("""
        INSERT INTO product_terms(
            offer_id, rate_pct, rate_kind, currency,
            amount_min, amount_max, term_months_min, term_months_max,
            fee_open, fee_service, grace_days, cashback_pct,
            early_withdraw, capitalization, replenishable,
            conditions, raw, source_snapshot_id, filter_context_id, digest,
            rate_min, rate_max, psk_min, psk_max)
        VALUES (:o,:r,:rk,:cur,:amn,:amx,:tmn,:tmx,:fo,:fs,:gd,:cb,:ew,:cap,:rep,
                :cond, CAST(:raw AS jsonb), :ssid, :fid, :dg,
                :rmin,:rmax,:pmin,:pmax)
        RETURNING terms_id
    """), {
        "o": offer_id, "r": d.rate_pct, "rk": d.rate_kind, "cur": d.currency,
        "amn": d.amount_min, "amx": d.amount_max,
        "tmn": d.term_months_min, "tmx": d.term_months_max,
        "fo": d.fee_open, "fs": d.fee_service,
        "gd": d.grace_days, "cb": d.cashback_pct,
        "ew": d.early_withdraw, "cap": d.capitalization, "rep": d.replenishable,
        "cond": d.conditions, "raw": json.dumps(d.raw, ensure_ascii=False, default=str),
        "ssid": snapshot_id, "fid": source_page_id, "dg": new_digest,
        "rmin": d.rate_min, "rmax": d.rate_max,
        "pmin": d.psk_min, "pmax": d.psk_max,
    }).scalar_one()

    if cur:
        # diff
        prev = session.execute(text("""
            SELECT rate_pct, rate_kind, currency, amount_min, amount_max,
                   term_months_min, term_months_max, fee_open, fee_service,
                   grace_days, cashback_pct,
                   early_withdraw, capitalization, replenishable, conditions
              FROM product_terms WHERE terms_id=:t
        """), {"t": cur[0]}).mappings().one()
        new_vals = {
            "rate_pct": d.rate_pct, "rate_kind": d.rate_kind, "currency": d.currency,
            "amount_min": d.amount_min, "amount_max": d.amount_max,
            "term_months_min": d.term_months_min, "term_months_max": d.term_months_max,
            "fee_open": d.fee_open, "fee_service": d.fee_service,
            "grace_days": d.grace_days, "cashback_pct": d.cashback_pct,
            "early_withdraw": d.early_withdraw, "capitalization": d.capitalization,
            "replenishable": d.replenishable, "conditions": d.conditions,
        }
        # Порог значимости: дрожь расчётных ставок в 3-4-м знаке (3.8544→3.8549)
        # — не событие; она давала ~12k мусорных строк/нед («14 тыс. изменений»
        # из фидбека аналитиков). Числовые поля сравниваем с допуском.
        _num_eps = {"rate_pct": 0.01, "fee_open": 0.5, "fee_service": 0.5,
                    "amount_min": 1.0, "amount_max": 1.0, "cashback_pct": 0.05}

        def _same(k, a, b):
            if a is None or b is None:
                return a is b
            eps = _num_eps.get(k)
            if eps is not None:
                try:
                    return abs(float(a) - float(b)) < eps
                except (TypeError, ValueError):
                    pass
            return str(a) == str(b)

        diff = {k: {"from": str(prev[k]) if prev[k] is not None else None,
                    "to": str(v) if v is not None else None}
                for k, v in new_vals.items() if not _same(k, prev[k], v)}
        if diff:                       # пустой дифф (только шум) — не событие
            session.execute(text("""
                INSERT INTO change_history(offer_id, prev_terms_id, new_terms_id, diff)
                VALUES (:o,:p,:n, CAST(:d AS jsonb))
            """), {"o": offer_id, "p": cur[0], "n": new_id,
                   "d": json.dumps(diff, ensure_ascii=False)})
    return offer_id, True

# Категории ежедневного sravni-сбора: только для них применимо протухание
# (bank_rating/npf/invest_broker собираются другими источниками и редко)
_DAILY_CATEGORIES = ("deposit", "credit", "mortgage", "card_credit",
                     "card_debit", "auto_loan", "metals", "microloan")


def expire_stale_offers(days: int = 3) -> int:
    """Деактивирует офферы, пропавшие из выдачи источника (аудит 22.07.2026:
    394 «вечно живых» вклада и весь metals с данными от 10 июня висели в
    витрине как актуальные). Вернувшийся оффер оживает в upsert_offer."""
    with db.session() as s:
        n = s.execute(text("""
            UPDATE product_offer SET is_active = false
             WHERE is_active
               AND category = ANY(CAST(:cats AS product_category[]))
               AND last_seen < now() - make_interval(days => :d)
        """), {"cats": list(_DAILY_CATEGORIES), "d": days}).rowcount
    log.info("[expire] деактивировано протухших офферов: %d", n)
    return n


def validate_offer_urls(limit: int = 80) -> dict:
    """HEAD/GET-проба ссылок активных офферов (случайная ротация — за пару дней
    прочёсывается весь пул): 404/410 → url=NULL, фронт покажет оффер без ссылки.
    Фикс жалобы аналитиков: клик по офферу (кейс ПСБ) вёл на 404."""
    import httpx
    from sqlalchemy import text as _t
    rows = []
    with db.session() as s:
        rows = [dict(r) for r in s.execute(_t("""
            SELECT offer_id, url FROM product_offer
            WHERE url IS NOT NULL AND is_active
            ORDER BY random() LIMIT :l"""), {"l": limit}).mappings().all()]
    bad: list[int] = []
    checked = 0
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
    with httpx.Client(timeout=6.0, follow_redirects=True, headers=ua) as c:
        for r in rows:
            checked += 1
            try:
                resp = c.head(r["url"])
                if resp.status_code in (403, 405):     # HEAD не любят — добиваем GET
                    resp = c.get(r["url"])
                if resp.status_code in (404, 410):
                    bad.append(r["offer_id"])
            except Exception:  # noqa: BLE001 — сетевой флак ≠ битая ссылка
                continue
    if bad:
        with db.session() as s:
            s.execute(_t("UPDATE product_offer SET url = NULL "
                         "WHERE offer_id = ANY(:ids)"), {"ids": bad})
    log.info("[url-check] проверено %d, битых %d", checked, len(bad))
    return {"checked": checked, "dead": len(bad)}


def normalize_batch(drafts: Iterable[OfferDraft], snapshot_id: int | None,
                    source_page_id: int | None,
                    source_name: str = "sravni_aggregator") -> dict:
    written = 0
    seen = 0
    with db.session() as s:
        for d in drafts:
            seen += 1
            _, changed = upsert_offer(s, d, snapshot_id, source_page_id, source_name)
            if changed:
                written += 1
    return {"seen": seen, "written": written}

def dedup_active_offers(session=None) -> int:
    """Гасит повторы одного продукта (банк + название в категории), оставляя
    самую свежую версию. Один вклад собирается семью таргетами sravni (регионы
    и суммы), каждый срез даёт свой external_id — для сравнения это один и тот
    же продукт, а в витрине он занимал семь строк (аудит 11.08.2026: 1378
    лишних строк, 1177 из них во вкладах). Зовётся после каждого сбора."""
    sql = text("""
        WITH live AS (
            SELECT o.offer_id, o.bank_id, o.category,
                   lower(regexp_replace(coalesce(o.title, ''), '[^[:alnum:]]', '', 'g')) AS k,
                   t.valid_from
              FROM product_offer o
              JOIN product_terms t ON t.offer_id = o.offer_id AND t.valid_to IS NULL
             WHERE o.is_active
        ), ranked AS (
            SELECT offer_id,
                   row_number() OVER (PARTITION BY bank_id, category, k
                                      ORDER BY valid_from DESC, offer_id DESC) AS rn
              FROM live WHERE k <> ''
        )
        UPDATE product_offer o SET is_active = false
          FROM ranked r WHERE o.offer_id = r.offer_id AND r.rn > 1
    """)
    if session is not None:
        return int(session.execute(sql).rowcount or 0)
    with db.session() as s:
        n = int(s.execute(sql).rowcount or 0)
    if n:
        log.info("дедуп витрины: погашено повторов %d", n)
    return n

# Ссылка источника прямо называет категорию продукта: /tracking-url?category=...
# Если она не совпадает с категорией, в которую оффер положен, это карточка
# кросс-промо («вам может подойти») — в автокредитах так жили 11 потребкредитов,
# и сберовский «На любые цели» был снят со страницы ВТБ (аудит 11.08.2026).
# Ждать expire_stale_offers нельзя: он держит оффер трое суток, защищая от
# временных сбоев источника, а чужой продукт неверен с первой секунды.
_URL_CAT_MAP = {"autocredits": "auto_loan", "mortgages": "mortgage",
                "credits": "credit", "creditcards": "card_credit",
                "debitcards": "card_debit", "deposits": "deposit"}


def expire_cross_promo() -> int:
    """Гасит офферы, чья ссылка указывает на другую категорию."""
    n = 0
    with db.session() as s:
        rows = s.execute(text("""
            SELECT offer_id, category, url FROM product_offer
             WHERE is_active AND url LIKE '%%category=%%'
        """)).all()
        bad = []
        for offer_id, category, url in rows:
            m = re.search(r"[?&]category=([a-z]+)", url or "")
            if not m:
                continue
            other = _URL_CAT_MAP.get(m.group(1))
            if other and other != category:
                bad.append(offer_id)
        if bad:
            n = s.execute(text("UPDATE product_offer SET is_active = false"
                               " WHERE offer_id = ANY(:ids)"), {"ids": bad}).rowcount
    if n:
        log.info("[expire] погашено кросс-промо чужой категории: %d", n)
    return n
