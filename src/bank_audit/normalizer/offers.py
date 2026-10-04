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

# Смена выдачи агрегатора (страница банка ↔ витрина с фильтром суммы) — не
# изменение условий: до закрепления выдачи ВТБ «Наличными» давал два таких
# «изменения» каждое утро. Историю не удаляем, а не считаем. Условия — для
# запросов к change_history ch (журнал «Рынка» и связка с «Отзывами»).
SAME_CTX_SQL = """NOT (p.raw->'filter_context' IS NOT NULL
                   AND n.raw->'filter_context' IS NOT NULL
                   AND p.raw->'filter_context' <> n.raw->'filter_context')"""
CTX_JOIN_SQL = """LEFT JOIN product_terms p ON p.terms_id = ch.prev_terms_id
          LEFT JOIN product_terms n ON n.terms_id = ch.new_terms_id"""
# Значимое изменение: в диффе есть не только ставка, либо ставка сдвинулась
# хотя бы на 0,01 п. п. (микрошум расчётных ставок не показываем)
SIGNIFICANT_CHANGE_SQL = """((SELECT count(*) FROM jsonb_object_keys(ch.diff) k
                          WHERE k <> 'rate_pct') > 0
                    OR abs(coalesce((ch.diff->'rate_pct'->>'to')::numeric, 0)
                         - coalesce((ch.diff->'rate_pct'->>'from')::numeric, 0)) >= 0.01)"""

# Откат: оффер вернулся к прежним условиям в течение 72 часов (условия «до»
# изменения A совпадают — с допусками записи — с условиями «после» более
# позднего изменения B того же оффера): A, B и всё между ними не изменения
# условий, а сбой или мигание выдачи. Окно — как у детектора «пилы» «Обзора» (aggregator._FLAP_H): сбор
# идёт раз в сутки, и возврат «туда-обратно» почти всегда разнесён по дням.
# Одно определение для журнала «Рынка», итогов выпуска, ИИ-аналитика и меток на
# графике жалоб — иначе экраны снова разойдутся в числах (аудит 03.10,
# ПЛТ-02/РЫН-05). :rev_days — глубина окна целыми сутками МСК; начало цепочки
# ищем ещё на 72 ч раньше, чтобы откат не обрезался на границе окна.
REVERT_WINDOW_H = 72
# Ключ изменения в журнале — время и номер одним числом: цепочка «до d»
# не должна задевать изменение с тем же временем, записанное после d.
_CH_KEY = ("(extract(epoch FROM {a}.changed_at)::numeric * 1000000 * 10000000000"
           " + {a}.change_id)")
# «Вернулось к прежнему» — те же поля и допуски, что у upsert_offer при
# записи изменения (_num_eps): дрожь расчётной ставки в 4-м знаке и «тихие»
# версии без строки журнала (поменялись только поля дайджеста из raw) не
# мешают узнать возврат; текст условий — точно.
_NEAR = "((tn.{f} IS NULL AND tp.{f} IS NULL) OR abs(tn.{f} - tp.{f}) < {e})"
_SAME_TERMS_SQL = "(" + " AND ".join(
    [_NEAR.format(f=f, e=e) for f, e in (("rate_pct", 0.01), ("amount_min", 1.0),
                                          ("amount_max", 1.0), ("fee_open", 0.5),
                                          ("fee_service", 0.5), ("cashback_pct", 0.05))]
    + [f"tn.{f} IS NOT DISTINCT FROM tp.{f}" for f in
       ("rate_kind", "currency", "term_months_min", "term_months_max", "grace_days",
        "early_withdraw", "capitalization", "replenishable", "conditions")]) + ")"
# Как считается: для каждого изменения c — самый поздний возврат d к условиям
# «до c» в пределах 72 ч (end_k); изменение x скрыто, если какое-то c ≤ x
# дотягивается до x (скользящий максимум end_k). Без разворачивания пар:
# у мигающих офферов пар сотни тысяч (1,3 с на 90 днях). Весь рынок за неделю —
# 14 мс, за 90 дней — 0,7 с: длинную историю считать по своим офферам
# (revert_ids_sql с условием на c.offer_id).
_REVERT_TMPL = """
    SELECT z.change_id FROM (
        SELECT e.change_id, e.k,
               max(e.end_k) OVER (PARTITION BY e.offer_id ORDER BY e.k
                                  ROWS UNBOUNDED PRECEDING) AS reach
          FROM (SELECT c.change_id, c.offer_id, """ + _CH_KEY.format(a="c") + """ AS k,
                       (SELECT max(""" + _CH_KEY.format(a="d") + """) FROM change_history d
                          JOIN product_terms tn ON tn.terms_id = d.new_terms_id
                         WHERE d.offer_id = c.offer_id
                           AND (d.changed_at, d.change_id) > (c.changed_at, c.change_id)
                           AND d.changed_at <= c.changed_at + interval '72 hours'
                           AND """ + _SAME_TERMS_SQL + """) AS end_k
                  FROM change_history c
                  JOIN product_terms tp ON tp.terms_id = c.prev_terms_id
                 WHERE c.changed_at >= (date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')
                                        - make_interval(days => CAST(:rev_days AS int)))
                                       AT TIME ZONE 'Europe/Moscow' - interval '72 hours'
                   /*SCOPE*/) e
        ) z
     WHERE z.reach >= z.k"""


def revert_ids_sql(offer_scope: str = "") -> str:
    """REVERT_IDS_SQL, суженный условием на c.offer_id (например,
    «c.offer_id = :o») — для длинных окон по одному офферу или банку."""
    return _REVERT_TMPL.replace("/*SCOPE*/", f"AND ({offer_scope})" if offer_scope else "")


REVERT_IDS_SQL = revert_ids_sql()

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


# ── одна карточка — несколько выдач ──────────────────────────────────────────
# Идентификатор продукта агрегатора не зависит от выдачи, а продукт собирается
# из нескольких: страница банка (общие условия) и витрины с фильтром суммы и
# региона (условия под эту сумму). ВТБ «Наличными»: страница банка — 19,9 % на
# 30 тыс.–7 млн, витрина «500 тыс. на 36 мес.» — 20,5 % на ступени 300 тыс.–1 млн.
# Обе писались в одну карточку, и каждое утро журнал получал два «изменения»
# туда и обратно (28 за две недели), а график ставки шёл пилой. Теперь у
# продукта закреплена одна выдача: другая не переписывает условия, пока
# закреплённая встречается в сборах. Переход — только на выдачу приоритетнее
# (страница банка > витрина Москвы > другие регионы) или когда закреплённую
# не видно дольше суток с запасом.
_CTX_STICKY_H = 36


def _ctx_rank(fc: dict | None) -> int:
    fc = fc or {}
    if fc.get("bank"):
        return 0
    return 1 if fc.get("region") in (None, "", "msk") else 2


def _ctx_of(d: OfferDraft) -> dict | None:
    raw = d.raw if isinstance(d.raw, dict) else {}
    fc = raw.get("filter_context")
    return fc if isinstance(fc, dict) and fc else None


def upsert_offer(session, d: OfferDraft, snapshot_id: int | None,
                 source_page_id: int | None,
                 source_name: str = "sravni_aggregator") -> tuple[int, bool]:
    """source_name — КТО принёс оффер. Раньше здесь стояла константа
    «sravni_aggregator», и все 530 предложений с banki.ru подписывались чужим
    именем: аудитор видел ссылку на banki.ru и подпись «источник sravni»,
    а происхождение числа доказать было нечем (аудит 11.08.2026)."""
    _fix_category(d)
    bank_id = None
    if d.category == "other" and (d.external_id or "").startswith("banki_rating_"):
        # Строка народного рейтинга несёт стабильный bankId площадки. Когда
        # banki.ru меняет написание («ТОЧКА» → «Точка Банк»), поиск банка по
        # имени заводил новую строку справочника, и банк расщеплялся надвое:
        # у обеих строк одно место и одни отзывы (аудит 03.10, ДАН-01). Тот же
        # bankId остаётся за тем банком, за которым уже числится.
        bank_id = session.execute(text("""
            SELECT bank_id FROM product_offer
             WHERE category = 'other' AND external_id = :e
             ORDER BY is_active DESC, last_seen DESC LIMIT 1"""),
            {"e": d.external_id}).scalar()
    if bank_id is None:
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
        SELECT terms_id, digest, raw->'filter_context',
               coalesce((raw->>'ctx_seen_at')::timestamptz, valid_from)
                   > now() - make_interval(hours => :sticky)
          FROM product_terms
         WHERE offer_id=:o AND valid_to IS NULL
         ORDER BY valid_from DESC LIMIT 1
    """), {"o": offer_id, "sticky": _CTX_STICKY_H}).first()

    if doubt:
        _flag_quality(session, offer_id, "implausible_value", doubt)

    new_fc = _ctx_of(d)
    cur_fc = cur[2] if cur and isinstance(cur[2], dict) and cur[2] else None
    if cur and cur[1] == new_digest:
        if new_fc is not None and new_fc == cur_fc:
            # закреплённая выдача подтверждена этим сбором
            session.execute(text("""
                UPDATE product_terms
                   SET raw = jsonb_set(coalesce(raw, '{}'::jsonb), '{ctx_seen_at}', to_jsonb(now()))
                 WHERE terms_id = :t
            """), {"t": cur[0]})
        return offer_id, False  # без изменений
    if (cur and new_fc is not None and cur_fc is not None and new_fc != cur_fc
            and _ctx_rank(new_fc) >= _ctx_rank(cur_fc) and cur[3]):
        return offer_id, False  # другая выдача того же продукта, не изменение условий

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

# Категории ежедневного сбора: для них применимо протухание по календарю.
# savings_account, npf и invest_broker — те же ежедневные сборы sravni и
# banki.ru (накопительный счёт — вклад с типом «накопительный»), а прежний
# комментарий «собираются редко» устарел: их строки не гасли никогда
# (аудит 03.10, ДАН-14). РКО и рейтинг — отдельными правилами ниже.
_DAILY_CATEGORIES = ("deposit", "credit", "mortgage", "card_credit",
                     "card_debit", "auto_loan", "metals", "microloan",
                     "savings_account", "npf", "invest_broker")

# Строки народного рейтинга banki.ru и тарифы РКО гаснут по ПРОГОНАМ своего
# сборщика: карточка, которой не было в K последних ПОЛНЫХ прогонах. Сломанный
# или оборванный сборщик (status 'partial', мало строк, неизменный снимок с
# нулём строк) ничего не гасит. Предохранитель откладывает массовое гашение —
# его разбирают руками.
_RATING_SWEEPS = 3
_RATING_MIN_ROWS = 200
_BREAKER_SHARE, _BREAKER_MIN = 0.25, 10
# (источник, категория, мин. строк в прогоне): РКО — переименованный тариф
# заводит новую карточку (ключ по имени), старая без правила жила бы вечно
_RUN_EXPIRY = (("banki_ratings", "other", _RATING_MIN_ROWS),
               ("sravni_rko", "rko", 250))


def _expire_by_runs(source: str, category: str, min_rows: int,
                    dry_run: bool = False) -> dict:
    with db.session() as s:
        runs = s.execute(text("""
            SELECT status, items_seen, started_at FROM extraction_run
             WHERE source = :src AND started_at > now() - interval '30 days'
             ORDER BY started_at DESC LIMIT :k"""), {"src": source, "k": _RATING_SWEEPS}).all()
        if (len(runs) < _RATING_SWEEPS
                or any(r[0] != "ok" or (r[1] or 0) < min_rows for r in runs)):
            return {"status": "skip_incomplete_runs", "expired": 0}
        cutoff = runs[-1][2]
        active = s.execute(text(
            "SELECT count(*) FROM product_offer WHERE category = CAST(:c AS product_category) "
            "AND is_active"), {"c": category}).scalar() or 0
        ids = [r[0] for r in s.execute(text("""
            SELECT offer_id FROM product_offer
             WHERE category = CAST(:cat AS product_category) AND is_active AND last_seen < :c"""),
            {"cat": category, "c": cutoff}).all()]
        if len(ids) > max(_BREAKER_MIN, _BREAKER_SHARE * active):
            log.warning("[expire] %s: гасить %d из %d — предохранитель, разобрать руками",
                        category, len(ids), active)
            return {"status": "blocked", "would_expire": len(ids), "active": int(active)}
        if ids and not dry_run:
            s.execute(text("""UPDATE product_offer SET is_active = false
                               WHERE offer_id = ANY(:ids) AND is_active AND last_seen < :c"""),
                      {"ids": ids, "c": cutoff})
    return {"status": "ok", "expired": len(ids), "dry_run": dry_run}


def expire_rating_rows(dry_run: bool = False) -> dict:
    """Протухание строк народного рейтинга (category='other'). 58 строк 03.10
    держали места выпавших и переименованных банков, и 52 номера мест
    повторялись дважды (ДАН-14)."""
    return _expire_by_runs("banki_ratings", "other", _RATING_MIN_ROWS, dry_run)


# Ежедневные категории: календарное правило «не видели 3 суток». Но если
# разом пропала заметная доля категории, это скорее сломанный сборщик (антибот,
# пропавший профиль браузера), чем рынок: гасим только то, чего нет уже
# _STALE_HARD_D суток — витрина не пустеет за три ночи сбоя, а совсем
# старые данные всё равно уходят.
_STALE_HARD_D = 14


def expire_stale_offers(days: int = 3) -> int:
    """Деактивирует офферы, пропавшие из выдачи источника (аудит 22.07.2026:
    394 «вечно живых» вклада и весь metals с данными от 10 июня висели в
    витрине как актуальные). Вернувшийся оффер оживает в upsert_offer."""
    n = 0
    with db.session() as s:
        stats = s.execute(text("""
            SELECT category::text, count(*) AS active,
                   count(*) FILTER (WHERE last_seen < now() - make_interval(days => :d)) AS stale
              FROM product_offer
             WHERE is_active AND category = ANY(CAST(:cats AS product_category[]))
             GROUP BY 1"""), {"cats": list(_DAILY_CATEGORIES), "d": days}).all()
        for cat, active, stale in stats:
            if not stale:
                continue
            d = days
            if stale > max(_BREAKER_MIN, _BREAKER_SHARE * active):
                log.warning("[expire] %s: не видели %d из %d — похоже на сбой сборщика, "
                            "гасим только старше %d сут.", cat, stale, active, _STALE_HARD_D)
                d = _STALE_HARD_D
                # флаг качества — чтобы сбой сборщика увидели до того, как через
                # две недели категория погаснет (раз в сутки на категорию)
                s.execute(text("""
                    INSERT INTO quality_flag (entity_type, entity_id, severity, code, detail)
                    SELECT 'category', 0, 'warn', 'EXPIRE_BLOCKED',
                           jsonb_build_object('category', CAST(:cat AS text),
                                              'stale', :st, 'active', :ac)
                     WHERE NOT EXISTS (SELECT 1 FROM quality_flag
                                        WHERE code = 'EXPIRE_BLOCKED'
                                          AND detail->>'category' = CAST(:cat AS text)
                                          AND created_at > now() - interval '20 hours')"""),
                          {"cat": cat, "st": int(stale), "ac": int(active)})
            n += s.execute(text("""
                UPDATE product_offer SET is_active = false
                 WHERE is_active AND category = CAST(:cat AS product_category)
                   AND last_seen < now() - make_interval(days => :d)
            """), {"cat": cat, "d": d}).rowcount
    log.info("[expire] деактивировано протухших офферов: %d", n)
    for src, cat, min_rows in _RUN_EXPIRY:
        try:
            log.info("[expire] %s по прогонам %s: %s", cat, src,
                     _expire_by_runs(src, cat, min_rows))
        except Exception as e:  # noqa: BLE001 — не роняет ночной цикл
            log.warning("[expire] %s: %s", cat, e)
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


def _collapse_in_run(drafts: list[OfferDraft]) -> tuple[list[OfferDraft], dict]:
    """Внутри прогона один ключ — одна версия.

    upsert_offer пишет черновики подряд в ОДНОЙ транзакции: две строки с одним
    ключом и разными условиями дают версию «туда» и версию «обратно» с одним
    временем — так 03.10 выглядели 50 из 50 последних изменений рынка (РКО).
    Адаптер РКО починен, но тот же класс ошибки возможен у любого источника
    (коллизия ключа, повтор банка между страницами), поэтому страж общий.
    Выбор не зависит от порядка выдачи: при разных условиях — вариант с
    наименьшим дайджестом, при одинаковых — с наименьшими (заголовок, ссылка)."""
    groups: dict[tuple, list[OfferDraft]] = {}
    for d in drafts:
        _fix_category(d)                  # категория может смениться (вклад → накопительный)
        key = (normalize_bank_key(d.bank_name_raw or ""), d.category, d.external_id)
        groups.setdefault(key, []).append(d)
    out, st = [], {"dup_rows": 0, "dup_conflicts": 0}
    for ds in groups.values():
        if len(ds) == 1:
            out.append(ds[0])
            continue
        st["dup_rows"] += len(ds) - 1
        if len({_digest(d) for d in ds}) > 1:
            st["dup_conflicts"] += 1
        out.append(min(ds, key=lambda d: (_digest(d), d.title or "", d.url or "")))
    return out, st


def normalize_batch(drafts: Iterable[OfferDraft], snapshot_id: int | None,
                    source_page_id: int | None,
                    source_name: str = "sravni_aggregator") -> dict:
    drafts = list(drafts)
    uniq, st = _collapse_in_run(drafts)
    if st["dup_rows"]:
        log.warning("[normalize] %s: повторов ключа за прогон %d (с разными условиями %d) "
                    "— записан один вариант", source_name, st["dup_rows"], st["dup_conflicts"])
    written = 0
    with db.session() as s:
        for d in uniq:
            _, changed = upsert_offer(s, d, snapshot_id, source_page_id, source_name)
            if changed:
                written += 1
    return {"seen": len(drafts), "written": written, **st}

def dedup_active_offers(session=None) -> int:
    """Гасит повторы одного продукта (банк + название в категории), оставляя
    самую свежую версию. Один вклад собирается семью таргетами sravni (регионы
    и суммы), каждый срез даёт свой external_id — для сравнения это один и тот
    же продукт, а в витрине он занимал семь строк (аудит 11.08.2026: 1378
    лишних строк, 1177 из них во вкладах). Зовётся после каждого сбора."""
    # У РКО одно название бывает у тарифа для ИП и для ООО, а «Оптимум» и
    # «Оптимум+» — разные пакеты: там в разбиение входят форма бизнеса и «+»,
    # иначе дедуп гасил бы настоящие тарифы (аудит 03.10, ПЛТ-02).
    sql = text("""
        WITH live AS (
            SELECT o.offer_id, o.bank_id, o.category,
                   CASE WHEN o.category = 'rko' THEN coalesce(o.sub_segment, '') ELSE '' END AS sub,
                   lower(regexp_replace(
                       CASE WHEN o.category = 'rko' THEN replace(coalesce(o.title, ''), '+', 'плюс')
                            ELSE coalesce(o.title, '') END,
                       '[^[:alnum:]]', '', 'g')) AS k,
                   t.valid_from
              FROM product_offer o
              JOIN product_terms t ON t.offer_id = o.offer_id AND t.valid_to IS NULL
             WHERE o.is_active
        ), ranked AS (
            SELECT offer_id,
                   row_number() OVER (PARTITION BY bank_id, category, sub, k
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
