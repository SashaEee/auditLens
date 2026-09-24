"""Поток новостей «Обзора»: заголовки, сводные посты, артефакты тарифов.

БД и модели не нужны: всё ниже — чистые функции.
"""
from datetime import datetime, timedelta, timezone

from bank_audit.digest.aggregator import rate_artifacts
from bank_audit.digest.newsflow import clean_title, split_roundup, title_ok


def test_clean_title_skips_emoji_and_hashtags():
    """24.09 в выпуск ушли «🔤 🔤 🔤» и «#БанковскийСектор» — первая строка поста."""
    t = clean_title("🔤 🔤 🔤 🔤\n#БанковскийСектор\n⚡️ Банк России повысил требования к капиталу с 1 октября")
    assert t == "Банк России повысил требования к капиталу с 1 октября"
    assert clean_title("‼️ ‼️ АФК «Система» меняет президента компании с понедельника").startswith("АФК")
    assert clean_title("«Любые изменения должны учитывать особенности спроса»").startswith("«")


def test_title_ok():
    assert not title_ok("#БанковскийСектор")
    assert not title_ok("🔤 🔤 🔤")
    assert title_ok("ЦБ оштрафовал четыре банка за нарушения")


def test_split_roundup():
    body = ("Важные новости, которые вы могли пропустить вчера:\n"
            "👉 Проект бюджета не повлияет на прогноз ЦБ по траектории ключевой ставки\n"
            "👉 ЦБ начал публиковать обезличенные данные о внебиржевых деривативах\n"
            "👉 Росфинмониторинг предложил новое основание для блокировки счетов граждан\n"
            "Frank Media в Telegram | MAX | Рассылка")
    subs = split_roundup({"url": "https://t.me/x/1", "body": body})
    assert len(subs) == 3
    assert subs[2]["title"].startswith("Росфинмониторинг")
    assert all(s["parent_url"] == "https://t.me/x/1" for s in subs)
    assert split_roundup({"url": "https://t.me/x/2", "body": "Обычный пост о ставках"}) == []


def test_rate_artifacts_flapping_and_jumps():
    """ВТБ «Наличными» каждое утро 19,9 → 20,5 → 19,9 за минуту — сбой парсера,
    а не изменение условий; он уходил в заголовки выпуска."""
    now = datetime.now(timezone.utc)
    rows, cid = [], 0
    for d in range(5, 0, -1):
        t = now - timedelta(days=d)
        cid += 1
        rows.append({"change_id": cid, "offer_id": 1, "changed_at": t, "from": 19.9, "to": 20.5})
        cid += 1
        rows.append({"change_id": cid, "offer_id": 1, "changed_at": t + timedelta(minutes=1),
                     "from": 20.5, "to": 19.9})
    cid += 1
    rows.append({"change_id": cid, "offer_id": 2, "changed_at": now - timedelta(hours=30),
                 "from": 11.9, "to": 7.9})           # подтверждённый скачок
    confirmed = cid
    cid += 1
    rows.append({"change_id": cid, "offer_id": 3, "changed_at": now - timedelta(hours=2),
                 "from": 21.2, "to": 10.9})          # свежий скачок — ждёт подтверждения
    fresh = cid
    cid += 1
    rows.append({"change_id": cid, "offer_id": 4, "changed_at": now - timedelta(hours=3),
                 "from": 13.5, "to": 13.7})          # обычное изменение
    flap, pending, offers = rate_artifacts(rows)
    assert len(flap) == 10
    assert pending == {fresh}
    assert confirmed not in flap and confirmed not in pending
    assert offers and offers[0]["offer_id"] == 1


def test_brief_items_drops_trailing_remarks():
    from bank_audit.digest.writer import brief_items
    md = ("- **[ВЫСОКИЙ]** **Чарджбэк** — рост ×3,6.\n  Аудитору: запросить выборку.\n\n---\n\n"
          "По жалобам без точного кода: пункт «Новое» не формируется.")
    assert brief_items(md) == "- **[ВЫСОКИЙ]** **Чарджбэк** — рост ×3,6.\n  Аудитору: запросить выборку."
    assert brief_items("Вступление без пунктов") is None
