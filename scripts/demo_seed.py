"""Демо-сид: генерирует ~1000 реалистичных банковских предложений по 7 категориям
   и ~800 отзывов, прогоняет нормализацию, quality checks, выводит аналитику.
   Позволяет увидеть весь pipeline без реального браузера."""
import json, random, hashlib
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from bank_audit.config import Settings
from bank_audit import db
from bank_audit.models import OfferDraft, ReviewDraft, FilterContext, RawSnapshot
from bank_audit.normalizer import offers as offers_norm, reviews as reviews_norm
from bank_audit.storage.raw_store import RawStore
from bank_audit.quality.checks import run_quality
from sqlalchemy import text

random.seed(42)

BANKS = [
    ("sberbank",   "Сбербанк",          True),
    ("vtb",        "ВТБ",               False),
    ("tinkoff",    "Тинькофф",          False),
    ("alfabank",   "Альфа-Банк",        False),
    ("gazprombank","Газпромбанк",        False),
    ("rshb",       "Россельхозбанк",    False),
    ("otkritie",   "Банк Открытие",     False),
    ("raiffeisen", "Райффайзенбанк",    False),
    ("pochtabank", "Почта Банк",        False),
    ("mkb",        "МКБ",               False),
    ("sovcombank", "Совкомбанк",        False),
    ("uralsib",    "УралСиб",           False),
]

CATEGORIES = [
    ("deposit",      "вклад",        (6.5, 21.0), (50_000, 10_000_000), (1, 36)),
    ("credit",       "кредит",       (11.9, 39.9),(50_000, 5_000_000),  (12, 84)),
    ("card_credit",  "кредитка",     (0.0, 29.9), (30_000, 1_000_000),  (12, 36)),
    ("card_debit",   "дебетовая",    (0.0, 0.0),  (0, 0),               (0, 0)),
    ("mortgage",     "ипотека",      (3.9, 16.5), (1_000_000, 30_000_000),(60, 360)),
    ("metals",       "драгметаллы",  (0.1, 4.5),  (1_000, 5_000_000),  (1, 60)),
    ("auto_loan",    "автокредит",   (8.9, 25.9), (300_000, 7_000_000), (12, 84)),
]

COMPLAINT_TOPICS_SAMPLE = [
    "Приложение зависает при входе, поддержка не отвечает",
    "Сняли комиссию без предупреждения, разбирались две недели",
    "Снизили ставку по вкладу через месяц, хотя говорили фиксированная",
    "Заблокировали карту перед отпуском, не объяснили причину",
    "Оформили страховку которую не просил, навязали условия",
    "Перевод висит третий день, поддержка говорит ждите",
    "Банкомат съел деньги, возврат занял месяц",
    "Ставка оказалась другой чем на сайте, скрытые условия",
]

POS_SAMPLE = [
    "Отлично, очень доволен сервисом, рекомендую всем",
    "Быстро одобрили, удобное приложение, спасибо банку",
    "Помогли решить вопрос, вежливые операторы",
    "Лучший банк для вклада, высокая ставка, удобно",
]


def seed_banks(session):
    for slug, name, is_sber in BANKS:
        session.execute(text("""
            INSERT INTO bank(slug, name, is_sber, aliases)
            VALUES (:s,:n,:b,:a)
            ON CONFLICT (slug) DO UPDATE SET name=EXCLUDED.name
        """), {"s": slug, "n": name, "b": is_sber, "a": [name.lower(), slug]})
    print(f"  ✓ {len(BANKS)} банков")


def make_offer(bank_slug: str, cat: str, cat_name: str,
               rate_range, amount_range, term_range, idx: int) -> OfferDraft:
    rmin, rmax = rate_range
    amt_min, amt_max = amount_range
    t_min, t_max = term_range
    # Чуть-чуть случайных вариаций у каждого банка
    rate = round(random.uniform(rmin, rmax), 2) if rmax > 0 else None
    ext_id = f"{bank_slug}_{cat}_{idx:04d}"
    title = f"{cat_name.capitalize()} «{['Выгодный','Надёжный','Стандартный','Премиум','Базовый'][idx%5]}»"
    return OfferDraft(
        bank_name_raw=bank_slug,
        category=cat,
        external_id=ext_id,
        title=title,
        url=f"https://www.sravni.ru/{cat}/{bank_slug}/{idx}/",
        rate_pct=Decimal(str(rate)) if rate else None,
        rate_kind="effective" if cat in ("deposit","metals") else "nominal",
        currency="RUB",
        amount_min=Decimal(str(amt_min)) if amt_min else None,
        amount_max=Decimal(str(amt_max)) if amt_max else None,
        term_months_min=t_min if t_min else None,
        term_months_max=t_max if t_max else None,
        raw={"demo": True, "filter_context": {"amount": 100000, "period": 12, "region": "msk"}},
    )


def seed_offers(raw_store: RawStore) -> int:
    total = 0
    # Фиктивный snapshot
    fake_html = b"<html><body>demo fixture</body></html>"
    path, sha, size = raw_store.write("demo", "seed", fake_html, "html")
    with db.session() as s:
        page_id = s.execute(text("""
            INSERT INTO source_page(source, url_norm, category, filter_context)
            VALUES ('demo','https://demo/seed','deposit','{}')
            ON CONFLICT (source, url_norm) DO UPDATE SET last_seen=now()
            RETURNING source_page_id
        """)).scalar_one()
        snap_id = s.execute(text("""
            INSERT INTO source_snapshot(source_page_id, fetched_at, http_status,
                                        content_sha256, storage_path, bytes)
            VALUES (:p, now(), 200, :sh, :pa, :b)
            ON CONFLICT (source_page_id, content_sha256) DO NOTHING
            RETURNING snapshot_id
        """), {"p": page_id, "sh": sha, "pa": path, "b": size}).scalar_one_or_none()

    drafts = []
    for bank_slug, _, _ in BANKS:
        for cat, cat_name, rr, ar, tr in CATEGORIES:
            for i in range(random.randint(1, 4)):  # 1-4 продукта банка в категории
                drafts.append(make_offer(bank_slug, cat, cat_name, rr, ar, tr, i))

    result = offers_norm.normalize_batch(drafts, snap_id, page_id)
    total = result["written"]
    print(f"  ✓ {len(drafts)} черновиков → {total} новых записей в product_terms")
    return total


def seed_reviews() -> int:
    drafts = []
    for bank_slug, bank_name, _ in BANKS:
        n_neg = random.randint(5, 25)
        n_pos = random.randint(3, 10)
        base_dt = datetime.now(timezone.utc) - timedelta(days=180)
        for i in range(n_neg):
            text_body = random.choice(COMPLAINT_TOPICS_SAMPLE) + f" (#{bank_slug}_{i})"
            drafts.append(ReviewDraft(
                source="banki_reviews",
                source_review_id=f"banki_{bank_slug}_{i}",
                source_url=f"https://www.banki.ru/services/responses/bank/{bank_slug}/response/{i}/",
                bank_name_raw=bank_slug,
                posted_at=base_dt + timedelta(days=random.randint(0,180)),
                rating=Decimal(str(round(random.uniform(1.0, 3.0), 1))),
                text=text_body,
                author_raw=f"User{i}",
            ))
        for i in range(n_pos):
            text_body = random.choice(POS_SAMPLE) + f" (#{bank_slug}_{i})"
            drafts.append(ReviewDraft(
                source="sravni_reviews",
                source_review_id=f"sravni_{bank_slug}_{i}",
                source_url=f"https://www.sravni.ru/bank/{bank_slug}/otzyvy/{i}/",
                bank_name_raw=bank_slug,
                posted_at=base_dt + timedelta(days=random.randint(0,180)),
                rating=Decimal(str(round(random.uniform(3.5, 5.0), 1))),
                text=text_body,
                author_raw=f"Happy{i}",
            ))

    result = reviews_norm.normalize_reviews(drafts, None)
    print(f"  ✓ {len(drafts)} отзывов → {result['written']} новых")
    return result["written"]


def show_analytics():
    with db.session() as s:
        # 1. Количество предложений по категориям
        rows = s.execute(text("""
            SELECT category, COUNT(*) as cnt,
                   ROUND(AVG(rate_pct),2) as avg_rate,
                   ROUND(MAX(rate_pct),2) as max_rate
              FROM v_offer_current
             GROUP BY category ORDER BY category
        """)).all()
        print("\n📊 Предложения по категориям:")
        print(f"  {'Категория':<16} {'Кол-во':>7} {'Ср.ставка':>10} {'Макс.ставка':>12}")
        for r in rows:
            print(f"  {r[0]:<16} {r[1]:>7} {str(r[2] or '-'):>10} {str(r[3] or '-'):>12}")

        # 2. Сбер vs рынок
        print("\n🏦 Сбер vs рынок (по категориям):")
        print(f"  {'Категория':<14} {'Сбер макс':>10} {'Рынок медиана':>14} {'Δ pp':>8}")
        rows = s.execute(text("SELECT * FROM v_sber_vs_market ORDER BY category")).all()
        for r in rows:
            cat, sber_max, sber_min, mmed, mmax, mmin, delta = r
            delta_s = f"{float(delta):+.2f}" if delta is not None else "—"
            print(f"  {cat:<14} {str(sber_max or '—'):>10} {str(mmed or '—'):>14} {delta_s:>8}")

        # 3. Топ-5 по ставке вклады
        print("\n🏆 Топ-5 вкладов по ставке:")
        rows = s.execute(text("""
            SELECT bank_name, title, rate_pct, amount_min, term_months_min
              FROM v_offer_top_by_rate WHERE category='deposit' AND rk<=5
             ORDER BY rk
        """)).all()
        for i, r in enumerate(rows, 1):
            print(f"  {i}. {r[0]:<20} {r[1]:<30} {r[2]}% от {r[3] or '?'} руб, {r[4] or '?'} мес")

        # 4. Жалобы по темам (топ-5)
        print("\n⚠️  Топ тем жалоб:")
        rows = s.execute(text("""
            SELECT topic, SUM(n) as total, ROUND(AVG(avg_rating),2) as avg_r
              FROM v_review_topics GROUP BY topic ORDER BY total DESC LIMIT 5
        """)).all()
        for r in rows:
            print(f"  {r[0]:<20} {r[1]:>5} упоминаний  avg_rating={r[2]}")

        # 5. Негативные отзывы топ-5 банков
        print("\n📉 Доля негативных отзывов (топ-5 по доле):")
        rows = s.execute(text("""
            SELECT bank_name, ROUND(neg_share*100,1), total
              FROM v_review_sentiment_share ORDER BY neg_share DESC NULLS LAST LIMIT 5
        """)).all()
        for r in rows:
            print(f"  {r[0]:<20} {str(r[1]):>5}% негатива  (всего {r[2]} отзывов)")

        # 6. Сбер: жалобы и темы
        print("\n🔍 Жалобы на Сбер:")
        rows = s.execute(text("""
            SELECT rt.topic, count(*) as n, round(avg(r.rating),2) avg_r
              FROM review r
              JOIN review_topic rt using(review_id)
              JOIN bank b using(bank_id)
             WHERE b.is_sber
             GROUP BY rt.topic ORDER BY n desc
        """)).all()
        if rows:
            for r in rows:
                print(f"  {r[0]:<20} {r[1]:>3} упоминаний  avg_rating={r[2]}")
        else:
            print("  (отзывов на Сбер нет)")

        # 7. История изменений
        total_changes = s.execute(text("SELECT count(*) FROM change_history")).scalar_one()
        print(f"\n📜 Записей в change_history: {total_changes}")


if __name__ == "__main__":
    settings = Settings.load()
    db.init(settings)
    raw_store = RawStore(settings.raw_dir)

    print("=== DEMO SEED ===")
    print("1. Загружаем справочник банков...")
    with db.session() as s:
        seed_banks(s)

    print("2. Генерируем предложения (~1000 продуктов)...")
    seed_offers(raw_store)

    print("3. Генерируем отзывы...")
    seed_reviews()

    print("4. Quality checks...")
    res = run_quality()
    print(f"  ✓ Отчёт: {res['report']}")
    print(f"  ✓ Флаги: {res['summary']}")

    print("5. Аналитика:")
    show_analytics()

    print("\n=== DONE ===")
