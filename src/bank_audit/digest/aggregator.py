"""SQL-секции дайджеста (0 токенов): числа детерминированы, LLM их не трогает.

  reviews_pulse — пульс жалоб: KPI 90 дн + недельные сигналы + топ растущих тем
                  + месячный тренд (всё делегируется в rag.reviews_dash, там кэш)
  tariff_moves  — изменения тарифов за 7 дн (change_history) + детект массового
                  движения + позиция Сбера + ключевая ставка (SOAP ЦБ)
  quality_ops   — доверие к данным: quality-флаги, свежесть сборов, капчи, объёмы
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

from sqlalchemy import text

from .. import db

log = logging.getLogger(__name__)


def _q(sql: str, params: dict | None = None) -> list[dict]:
    with db.session() as s:
        return [dict(r) for r in s.execute(text(sql), params or {}).mappings().all()]


def _scalar(sql: str, params: dict | None = None):
    with db.session() as s:
        return s.execute(text(sql), params or {}).scalar()


def _fnum(v) -> float | None:
    """Значения в change_history.diff — строки (бывают NULL/мусор)."""
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


# ── reviews_pulse ─────────────────────────────────────────────────────────────

async def reviews_pulse(day: date) -> dict:
    from ..rag import reviews_dash as rd

    def _compute():
        bank = "Сбербанк"
        ov = rd.overview(bank) or {}
        wk = rd.weekly_signals(bank) or {}
        try:
            # журнал сигналов: всплески, попавшие в «Обзор», — эпизодом со снимком
            from ..rag import reviews_work
            reviews_work.record_signals(wk, bank, None, min_interval_s=0)
        except Exception as e:  # noqa: BLE001 — выпуск не зависит от журнала
            log.warning("журнал сигналов: %s", e)
        th = rd.themes(bank) or {}
        tr = rd.trend(bank) or {}
        # топ растущих тем: только значимо обгоняющие общий поток жалоб (Б4) —
        # «+77%» на 99 жалобах при общем росте +23% в пределах колебаний
        themes_up = [t for t in (th.get("themes") or [])
                     if t.get("key") != "other" and t.get("delta_sig")
                     and (t.get("excess") or 0) > 0
                     and (t.get("delta_pct") or 0) >= 50 and (t.get("n") or 0) >= 30][:5]
        series = (tr.get("series") or [])[-8:]
        # «пульс дня» на главной: расхождение с рынком (есть всегда, в отличие
        # от пороговых сигналов) и слепая зона классификатора
        wp = rd.week_pulse(bank) or {}
        unc = rd.unclassified_week(bank) or {}
        return {
            # версия методики: «ко вчера» сравнивает только снимки одной версии —
            # 25.09 вчерашний снимок был по старым меткам, и «+5,5 пп эскалации»,
            # «−30 вне кодификатора» были сменой счёта, а не событием
            "method": f"annotation:{rd._ann_schema()}",
            "kpi": {k: ov.get(k) for k in
                    ("total", "prev", "delta_pct", "delta_low_n", "market_share_pct",
                     "market_rank", "market_banks", "escalation_pct", "as_of",
                     "escalation_filed_pct", "market_escalation_pct", "escalation_sig")},
            "signals": wk.get("signals") or [],
            "overall": wk.get("overall") or {},
            "themes_up": themes_up,
            "diverge": wp.get("diverge") or [],
            "unclassified": unc,
            "trend": series,
            "checked": {"themes": len((th.get("themes") or [])),
                        "signals": len(wk.get("signals") or [])},
        }
    return await asyncio.to_thread(_compute)


# ── tariff_moves ──────────────────────────────────────────────────────────────

# Поля diff, которые считаем «значимыми» изменениями тарифа (текстовые диффы
# conditions шумят при переездах агрегатора).
_RATE_FIELDS = ("rate_pct", "fee_service", "fee_open")


# Артефакты сбора тарифов. Замер 24.09.2026: ставка ВТБ «Наличными» каждое
# утро «менялась» 19,9 → 20,5 → 19,9 за минуту (на странице две цены для разных
# сумм, парсер берёт то одну, то другую), и это уходило в заголовки выпуска:
# «ВТБ дважды за сутки качнул ставку», «ВТБ утроил ставку по кредитам».
_FLAP_H = 72.0          # вернулось к прежнему значению за это время — не изменение
_JUMP_PP = 3.0          # скачок крупнее — только после подтверждения следующим сбором
_JUMP_CONFIRM_H = 20.0  # подтверждение: прошёл сбор, и значение не откатилось


def rate_artifacts(rows: list[dict]) -> tuple[set, set, list[dict]]:
    """По изменениям ставок за ~10 дней: (флапающие change_id, неподтверждённые
    скачки change_id, сводка по офферам-«мигалкам» для «Пульса»).

    rows — {change_id, offer_id, changed_at, from, to, bank, title} в любом порядке."""
    from collections import defaultdict
    by_offer: dict = defaultdict(list)
    for r in rows:
        if r.get("from") is None or r.get("to") is None:
            continue
        by_offer[r["offer_id"]].append(r)
    flap, pending, offers = set(), set(), []
    now = datetime.now(timezone.utc)
    for oid, seq in by_offer.items():
        seq.sort(key=lambda x: x["changed_at"])
        n_flap = 0
        for i, c in enumerate(seq):
            for d in seq[i + 1:]:
                gap_h = (d["changed_at"] - c["changed_at"]).total_seconds() / 3600.0
                if gap_h > _FLAP_H:
                    break
                if abs(d["to"] - c["from"]) < 0.005:        # A→B … →A
                    flap.update((c["change_id"], d["change_id"]))
                    n_flap += 1
                    break
        # мигание между двумя значениями без чистого возврата «в пару»
        vals = {round(v, 2) for c in seq for v in (c["from"], c["to"])}
        if len(seq) >= 3 and len(vals) <= 2:
            flap.update(c["change_id"] for c in seq)
            n_flap = max(n_flap, len(seq) // 2)
        if n_flap:
            offers.append({"bank": seq[0].get("bank"), "title": seq[0].get("title"),
                           "offer_id": oid, "flaps": n_flap,
                           "values": sorted(vals)[:4]})
        last = seq[-1]
        for c in seq:
            if c["change_id"] in flap or abs(c["to"] - c["from"]) < _JUMP_PP:
                continue
            ts = c["changed_at"] if c["changed_at"].tzinfo else c["changed_at"].replace(tzinfo=timezone.utc)
            age_h = (now - ts).total_seconds() / 3600.0
            # подтверждён: это последнее изменение оффера (не откатилось) и
            # после него прошёл хотя бы один утренний сбор
            if not (c is last and age_h >= _JUMP_CONFIRM_H):
                pending.add(c["change_id"])
    offers.sort(key=lambda o: -o["flaps"])
    return flap, pending, offers


async def tariff_moves(day: date) -> dict:
    def _compute():
        rows = _q("""
            SELECT b.name AS bank, b.slug AS bank_slug, b.is_sber,
                   o.category, o.title, o.url,
                   ch.change_id, ch.offer_id, ch.diff, ch.changed_at
              FROM change_history ch
              JOIN product_offer o USING (offer_id)
              JOIN bank b USING (bank_id)
              LEFT JOIN product_terms p ON p.terms_id = ch.prev_terms_id
              LEFT JOIN product_terms n ON n.terms_id = ch.new_terms_id
             WHERE ch.changed_at > now() - interval '10 days'
               -- смена выдачи агрегатора — не изменение условий
               AND NOT (p.raw->'filter_context' IS NOT NULL
                        AND n.raw->'filter_context' IS NOT NULL
                        AND p.raw->'filter_context' <> n.raw->'filter_context')
             ORDER BY ch.changed_at DESC
             LIMIT 3000
        """)
        import json as _json

        def _diff(r):
            d = r.get("diff") or {}
            if isinstance(d, str):
                try:
                    d = _json.loads(d)
                except Exception:
                    d = {}
            return d

        seq = []
        for r in rows:
            rate = _diff(r).get("rate_pct") or {}
            f, t = _fnum(rate.get("from")), _fnum(rate.get("to"))
            if f is not None and t is not None:
                ts = r["changed_at"] if r["changed_at"].tzinfo else r["changed_at"].replace(tzinfo=timezone.utc)
                seq.append({"change_id": r["change_id"], "offer_id": r["offer_id"],
                            "changed_at": ts, "from": f, "to": t,
                            "bank": r["bank"], "title": r["title"]})
        flap, pending, flap_offers = rate_artifacts(seq)
        week_ago = datetime.now(timezone.utc).timestamp() - 7 * 86400
        top, by_bank, cat_48h = [], {}, {}
        for r in rows:
            ts0 = r["changed_at"] if r["changed_at"].tzinfo else r["changed_at"].replace(tzinfo=timezone.utc)
            if ts0.timestamp() < week_ago:
                continue                  # 10 дней — только для детекта мигания
            if r["change_id"] in flap:
                continue                  # сбой сбора, а не изменение условий
            diff = r.get("diff") or {}
            if isinstance(diff, str):
                import json as _json
                try:
                    diff = _json.loads(diff)
                except Exception:
                    diff = {}
            rate = diff.get("rate_pct") or {}
            f, t = _fnum(rate.get("from")), _fnum(rate.get("to"))
            key = (r["bank"], r["category"])
            bb = by_bank.setdefault(key, {"bank": r["bank"], "is_sber": bool(r["is_sber"]),
                                          "category": r["category"], "n": 0, "n_rate": 0})
            bb["n"] += 1
            if any(fld in diff for fld in _RATE_FIELDS):
                bb["n_rate"] += 1
            if (f is not None and t is not None and abs(t - f) >= 0.05
                    and r["change_id"] not in pending):
                top.append({"bank": r["bank"], "is_sber": bool(r["is_sber"]),
                            "category": r["category"], "title": (r["title"] or "")[:90],
                            "from": f, "to": t, "delta": round(t - f, 2),
                            "changed_at": r["changed_at"].isoformat(),
                            # точные диплинки «Обзор → конкретное изменение»
                            "bank_slug": r.get("bank_slug"),
                            "offer_id": r.get("offer_id"),
                            "change_id": r.get("change_id")})
                # окно 48ч для детекта массового движения (возраст — в python,
                # НЕ отдельным SQL на строку)
                ts = r["changed_at"]
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
                if age_h <= 48.0:
                    cat_48h.setdefault(r["category"], set()).add(r["bank"])
        top.sort(key=lambda x: abs(x["delta"]), reverse=True)
        top = top[:15]

        # массовое движение: ≥3 банков сменили ставки одной категории за 48 ч
        mass = [{"category": c, "banks": sorted(bs), "n_banks": len(bs), "window_h": 48}
                for c, bs in cat_48h.items() if len(bs) >= 3]
        mass.sort(key=lambda m: -m["n_banks"])

        # сбор после паузы: диффы кластеризуются в момент прогона → массовость
        # может быть артефактом сбора, честно помечаем. extraction_run хранит
        # строку на КАЖДЫЙ target — меряем разрыв между БАТЧАМИ (последний
        # батч = всё в пределах 2 ч от максимума), а не между соседними строками
        gap_days = _scalar("""
            WITH m AS (SELECT max(finished_at) AS mx FROM extraction_run
                        WHERE status = 'ok' AND finished_at IS NOT NULL)
            SELECT extract(epoch FROM (SELECT mx FROM m) - max(er.finished_at)) / 86400.0
              FROM extraction_run er, m
             WHERE er.status = 'ok' AND er.finished_at IS NOT NULL
               AND er.finished_at < m.mx - interval '2 hours'
        """)
        after_pause = bool(gap_days is not None and float(gap_days) > 3.0)

        sber_gap = _q("SELECT * FROM v_sber_vs_market ORDER BY category")
        for r in sber_gap:
            for k, v in list(r.items()):
                if v is not None and k != "category":
                    try:
                        r[k] = float(v)
                    except (TypeError, ValueError):
                        pass

        return {
            "top_changes": top,
            # сбои сбора тарифов: в выпуск не идут, показываются в «Пульсе»
            "artifacts": {"flapping_changes": len(flap), "pending_jumps": len(pending),
                          "offers": flap_offers[:10]},
            "by_bank": sorted(by_bank.values(), key=lambda x: -x["n"])[:10],
            "mass_updates": mass,
            "after_pause": after_pause,
            "sber_gap": sber_gap,
            "totals": {
                # СОБЫТИЯ, не строки: считаем офферы со значимым изменением
                # (порог 0.01 п.п. — как в normalizer; исторический микрошум
                # 3-4-го знака давал «14 тыс. изменений» — фидбек аналитиков)
                "changes_7d": int(_scalar("""
                    SELECT count(DISTINCT ch.offer_id) FROM change_history ch
                     WHERE ch.changed_at > now()-interval '7 days'
                       AND ((SELECT count(*) FROM jsonb_object_keys(ch.diff) k
                              WHERE k <> 'rate_pct') > 0
                            OR abs(coalesce((ch.diff->'rate_pct'->>'to')::numeric, 0)
                                 - coalesce((ch.diff->'rate_pct'->>'from')::numeric, 0)) >= 0.01)
                    """) or 0),
                "banks_changed_7d": int(_scalar("""
                    SELECT count(DISTINCT b.bank_id) FROM change_history ch
                      JOIN product_offer o USING (offer_id) JOIN bank b USING (bank_id)
                     WHERE ch.changed_at > now()-interval '7 days'
                       AND ((SELECT count(*) FROM jsonb_object_keys(ch.diff) k
                              WHERE k <> 'rate_pct') > 0
                            OR abs(coalesce((ch.diff->'rate_pct'->>'to')::numeric, 0)
                                 - coalesce((ch.diff->'rate_pct'->>'from')::numeric, 0)) >= 0.01)
                    """) or 0),
                # изменения самого Сбера — для плитки пульса вместо «флагов качества»
                "sber_changes_7d": int(_scalar("""
                    SELECT count(DISTINCT ch.offer_id) FROM change_history ch
                      JOIN product_offer o USING (offer_id) JOIN bank b USING (bank_id)
                     WHERE b.is_sber AND ch.changed_at > now()-interval '7 days'
                       AND ((SELECT count(*) FROM jsonb_object_keys(ch.diff) k
                              WHERE k <> 'rate_pct') > 0
                            OR abs(coalesce((ch.diff->'rate_pct'->>'to')::numeric, 0)
                                 - coalesce((ch.diff->'rate_pct'->>'from')::numeric, 0)) >= 0.01)
                    """) or 0),
                "banks_tracked": int(_scalar(
                    "SELECT count(DISTINCT bank_id) FROM product_offer WHERE is_active") or 0),
                "last_change_at": (_scalar("SELECT max(changed_at) FROM change_history") or None),
                "last_ok_run": (_scalar(
                    "SELECT max(finished_at) FROM extraction_run WHERE status='ok'") or None),
            },
        }

    out = await asyncio.to_thread(_compute)
    for k in ("last_change_at", "last_ok_run"):
        v = out["totals"].get(k)
        if v is not None and not isinstance(v, str):
            out["totals"][k] = v.isoformat()

    # ключевая ставка — отдельный best-effort fetch (SOAP ЦБ, кэш 1 ч, история
    # в cbr_key_rate); ЦБ недоступен → берётся последнее известное из БД
    try:
        from .news import fetch_key_rate
        out["key_rate"] = await asyncio.to_thread(fetch_key_rate)
        # с какой даты действует текущее значение: «ключевая 14% с 24.09» писало
        # дату выгрузки ряда, а не решения ЦБ
        kr_ = out["key_rate"] or {}
        pts = kr_.get("points") or []
        if pts:
            i = len(pts) - 1
            while i > 0 and pts[i - 1].get("rate") == pts[-1].get("rate"):
                i -= 1
            kr_["since"] = pts[i].get("date") if i > 0 else None
    except Exception as e:  # noqa: BLE001
        log.info("key_rate fetch failed: %s", e)
        out["key_rate"] = None

    # спред «макс. вклад Сбера − ключевая» (для пульса)
    try:
        kr = (out.get("key_rate") or {}).get("current")
        dep = next((r for r in out["sber_gap"] if r.get("category") == "deposit"), None)
        if kr is not None and dep and dep.get("sber_max") is not None:
            out["dep_spread_pp"] = round(float(dep["sber_max"]) - float(kr), 2)
    except Exception:  # noqa: BLE001
        pass
    return out


# ── quality_ops ───────────────────────────────────────────────────────────────

async def quality_ops(day: date) -> dict:
    def _compute():
        flags = _q("""
            SELECT code, severity, count(*) n FROM quality_flag
             WHERE created_at > now() - interval '1 day'
             GROUP BY code, severity ORDER BY n DESC LIMIT 20
        """)
        runs = _q("""
            SELECT DISTINCT ON (source) source, status, finished_at, error
              FROM extraction_run ORDER BY source, started_at DESC
        """)
        for r in runs:
            if r.get("finished_at") is not None:
                r["finished_at"] = r["finished_at"].isoformat()
            if r.get("error"):
                r["error"] = str(r["error"])[:160]
        captcha_n = 0
        try:
            import json as _json
            from ..config import Settings
            p = Settings.load().workspace_dir / "captcha_pending.json"
            if p.exists():
                captcha_n = len(_json.loads(p.read_text()) or [])
        except Exception:  # noqa: BLE001
            pass
        return {
            "flags": flags,
            "flags_err": sum(f["n"] for f in flags if f["severity"] == "error"),
            "flags_warn": sum(f["n"] for f in flags if f["severity"] == "warn"),
            "runs": runs,
            "captcha_pending": captcha_n,
            "totals": {
                "banks": int(_scalar("SELECT count(*) FROM bank") or 0),
                "offers": int(_scalar("SELECT count(*) FROM product_offer WHERE is_active") or 0),
            },
        }
    return await asyncio.to_thread(_compute)
