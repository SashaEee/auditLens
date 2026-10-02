"""LLM-объяснение аномалий/пиков по выборке реальных жалоб (on-demand, по кнопке).

Не классифицирует корпус и не трогает горячий путь — вызывается только когда
аудитор нажал «Объяснить» на гео-аномалии или пике динамики. Возвращает
человекочитаемую прозу (без JSON-парсинга → устойчиво к провайдеру, который не
поддерживает response_format=json_object).
"""
from __future__ import annotations

import logging
import re

from openai import AsyncOpenAI

from ..ai.analyst import (LLM_API_KEY, LLM_BASE_URL, fast_model, insight_model,
                          smart_model)
from ..ai.llm_utils import _patch_client_reasoning_effort

log = logging.getLogger(__name__)


def _client() -> AsyncOpenAI:
    c = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, max_retries=2, timeout=60)
    return _patch_client_reasoning_effort(c)   # reasoning_effort=low — иначе thinking съедает ответ

_SYSTEM = (
    "Ты — аналитик службы внутреннего аудита Сбербанка. Тебе дают выборку реальных "
    "негативных жалоб клиентов (banki.ru) по конкретному срезу (город или месяц с "
    "всплеском). Кратко и по делу объясни, ЧТО вероятно стоит за этим всплеском/"
    "аномалией и НА ЧТО обратить внимание аудитору. Только то, что подтверждается "
    "текстами — не выдумывай фактов, цифр и причин сверх жалоб. 3–5 предложений, "
    "деловой тон, без воды и без маркетинга."
)


async def explain_segment(seg: dict, *, label: str, profile: dict | None = None) -> str | None:
    """seg — reviews_dash.segment_reviews(), profile — segment_profile(): чем
    срез отличается от нормы. Модель сначала получает цифры, потом тексты, и не
    называет аномалией срез, который правилами вкладки ею не отмечен."""
    texts = (seg or {}).get("texts") or []
    if not texts:
        return None
    prof = profile or {}
    rows = prof.get("rows") or []
    table = "\n".join(f"  — {r['label']}: {r['n']} ({r['pct']}% против {r['base_pct']}% в норме, ×{r['index']})"
                      for r in rows) or "  —"
    flag = ("Срез ОТМЕЧЕН вкладкой как аномалия." if prof.get("flagged") else
            "Срез НЕ отмечен вкладкой как аномалия: не называй его аномалией или всплеском, "
            "опиши, чем структура жалоб отличается от нормы.")
    joined = "\n\n".join(f"— {t}" for t in texts[:20])
    user = (
        f"Срез: {label}. Жалоб в срезе: {prof.get('n') or seg.get('n')}; норма — {prof.get('base_label') or '—'}.\n"
        f"{flag}\n"
        f"Главные проблемы среза против нормы (посчитано кодом, числа не меняй):\n{table}\n\n"
        f"Жалобы клиентов (выборка):\n{joined}\n\n"
        "Дай аудитору коротко, по-русски: (1) чем срез отличается от нормы — опираясь на "
        "таблицу; (2) 2–3 повторяющихся сюжета из жалоб своими словами; (3) что конкретно "
        "проверить. Не больше 180 слов, без вступления."
    )
    try:
        from ..digest.writer import _chat
        md, _ti, _to = await _chat(insight_model(), _SYSTEM, user, max_tokens=5000)
        return md or None
    except Exception as e:  # noqa: BLE001 — деградируем мягко, объяснение не критично
        log.warning("reviews_llm.explain_segment упал: %s", e)
        return None


# ── LLM-классификация показанных отзывов (on-demand, по кнопке) ──────────────
# Гибкий подход: LLM сам формулирует КОНКРЕТНУЮ тему обращения (free-form, не из
# фикс. списка) — ловит «Блокировка по 161-ФЗ», «Карта СВОи», «Навязанная страховка
# по ипотеке» и т.п., что хардкод-таксономия пропускает. risk-класс — для цвета.
_CLS_SYSTEM = (
    "Ты — аналитик внутреннего аудита банка. Для каждой жалобы клиента сформулируй "
    "КОНКРЕТНУЮ суть обращения короткой темой (2–5 слов: продукт + проблема, при "
    "наличии — закон/норматив). Учитывай смысл и отрицания: «не навязывали» — НЕ "
    "навязывание; «спасибо, разблокировали» — не блокировка. Не обобщай до «обслуживание»."
)
_RISKS = ("compliance", "conduct", "ops")


async def classify_reviews(items: list[dict]) -> list[dict | None]:
    """On-demand LLM-классификация ~20 показанных отзывов в КОНКРЕТНЫЕ темы (free-form).
    Возвращает по индексам {themes:[{short,label,risk}]} или None (None → regex-fallback)."""
    texts = [(it.get("text") or "")[:600] for it in items]
    if not texts:
        return []
    listing = "\n".join(f"#{i+1}: {t}" for i, t in enumerate(texts))
    user = (
        "Для КАЖДОЙ жалобы дай: (1) конкретную тему (2–5 слов, напр. «Блокировка по "
        "161-ФЗ», «Навязанная страховка по кредиту», «Карта СВОи: отказ», «Двойное "
        "списание по СБП», «Сбой в приложении»); (2) risk-класс: compliance "
        "(регуляторика/закон/ЦБ/суд), conduct (недобросовестные практики к клиенту), "
        "ops (операционные сбои/сервис).\n"
        "Формат СТРОГО по одной строке на жалобу, без лишнего:\n<номер> | <тема> | <risk>\n"
        "Пример:\n3 | Блокировка по 161-ФЗ | compliance\n\n"
        f"Жалобы:\n{listing}"
    )
    out: list[dict | None] = [None] * len(texts)
    try:
        resp = await _client().chat.completions.create(
            model=fast_model(),
            messages=[{"role": "system", "content": _CLS_SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.0, max_tokens=2000)
        content = resp.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001
        log.warning("reviews_llm.classify_reviews упал: %s", e)
        return out
    for line in content.splitlines():
        m = re.match(r"\s*#?(\d+)\s*[|:.\)]\s*(.+)", line)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if not (0 <= idx < len(texts)):
            continue
        rest = m.group(2).strip()
        risk = next((rc for rc in _RISKS if rc in rest.lower()), "other")
        topic = rest.split("|")[0].strip()
        topic = re.sub(r"[|\-—:]+\s*(compliance|conduct|ops)\b.*$", "", topic, flags=re.I).strip(" .—-|:")
        if topic:
            out[idx] = {"themes": [{"short": topic[:46], "label": topic, "risk": risk}]}
    return out


def signal_lines(sig: dict) -> tuple[list[str], str]:
    """Строки сигналов с точными числами и строка общего объёма недели."""
    lines = []
    for s in (sig or {}).get("signals") or []:
        bits = []
        if s.get("new"):
            bits.append("НОВАЯ проблема (раньше почти не было)")
        elif s.get("ratio"):
            bits.append(f"×{s['ratio']} к норме ~{s['baseline_week']}/нед")
        if s.get("accel"):
            bits.append(f"ускоряется (нед: {s.get('prev_week')}→{s['week']})")
        mr = s.get("market_ratio")
        # «только у банка» — лишь при ровном рынке; иначе во сколько раз сильнее
        # (раньше при рынке ×2,09 модели писали «ТОЛЬКО у банка», и она
        # повторяла это в заголовке)
        if s.get("bank_specific") or (mr is not None and mr >= 1.4):
            from .reviews_dash import market_flat, market_phrase
            note = market_phrase(s.get("ratio"), mr)
            if note and market_flat(mr):
                bits.append("ТОЛЬКО у банка — " + note.split(": ", 1)[-1])
            elif note and s.get("bank_specific"):
                bits.append(f"у банка {note}")
            elif mr is not None:
                bits.append(f"рынок тоже растёт ×{mr} (возможно отраслевое)")
        if s.get("geo"):
            bits.append(f"{s['geo']['share']}% из г. {s['geo']['city']}")
        lines.append(f'- {s["label"]} [{s.get("level", "medium")}]: {s["week"]} за 7 дн; '
                     + "; ".join(bits))
    ov = (sig or {}).get("overall") or {}
    ov_line = (f'Всего жалоб за неделю: {ov.get("week")} (обычно ~{ov.get("baseline_week")}/нед'
               + (f', рынок ×{ov["market_ratio"]}' if ov.get("market_ratio") is not None else "")
               + ").") if ov.get("week") is not None else ""
    return lines, ov_line


def signal_context(sig: dict, bank: str, product: str | None = None,
                   per_signal: int = 12) -> str:
    """Жалобы, из которых сложился КАЖДЫЙ сигнал, — отдельно по сигналам, с
    изложением и дословной цитатой; плюс жалобы недели вне кодификатора.

    Синхронная (ходит в базу) — звать через asyncio.to_thread. Модель видит
    только жалобы своего сигнала и не может перенести в объяснение формулировку
    из отзыва другой темы: так 22.09 в заголовок попало «навязывание платной
    карты без согласия» из чужого отзыва о событии 2025 года."""
    from . import reviews_dash as rd
    blocks = []
    for s in ((sig or {}).get("signals") or [])[:4]:
        ev = rd.signal_evidence(bank, s["key"], product=product, limit=per_signal) or []
        items = []
        for e in ev:
            q = f" — «{e['quote'][:200]}»" if e.get("quote") else ""
            items.append(f"  — {e.get('date') or ''}: {e.get('summary') or ''}{q}")
        blocks.append(f"ЖАЛОБЫ СИГНАЛА «{s['label']}» (показано {len(items)} из {s['week']}):\n"
                      + ("\n".join(items) or "  —"))
    clusters = rd.novel_clusters(bank, product=product) or []
    nov = []
    for k, c in enumerate(clusters[:3], 1):
        ex = "\n".join(f"    — {x['new_topic']}: {(x.get('summary') or '')[:220]}" for x in c["items"][:6])
        nov.append(f"  СЮЖЕТ {k} ({c['n']} жалоб):\n{ex}")
    return ("\n\n".join(blocks)
            + "\n\nНОВЫЕ СЮЖЕТЫ ВНЕ КОДИФИКАТОРА (сгруппированы кодом по сходству; других "
              "«новых» не выдумывай):\n"
            + ("\n".join(nov) if nov else "  нет — пункт «Новое» не пиши"))


# ── Срочные аномалии за 7 дней (audit-радар, on-demand при загрузке блока) ───
_ANOM_SYSTEM = (
    "Ты — старший аналитик службы внутреннего аудита банка. НЕ пересказывай жалобы — "
    "дай АНАЛИЗ: что аномально, почему важно, куда смотреть и насколько срочно. Тебе "
    "дают точные недельные метрики (НЕ меняй числа) и свежие жалобы. Сигналы, на "
    "которые опирайся:\n"
    "• рост темы к норме (×N) и УСКОРЕНИЕ (растёт неделя-к-неделе) — проблема нарастает;\n"
    "• «только у банка» = всплеск у банка, а рынок по теме ровный → это НАША регрессия "
    "(высокий приоритет); если рынок тоже растёт — вероятно отраслевое/сезонное (ниже);\n"
    "• гео-концентрация (≥40% в одном городе) → локальный сбой (отделение/банкомат/регион);\n"
    "• жалобы без точного кода кодификатора → свежий инцидент, которого ещё нет в списке проблем.\n"
    "Не алармируй без чисел; без эмодзи."
)


def _brief_format() -> str:
    from ..digest.writer import _BRIEF_FORMAT
    return _BRIEF_FORMAT


async def anomaly_brief(sig: dict, context: str) -> str | None:
    """sig — reviews_dash.weekly_signals(); context — signal_context(). Возвращает
    markdown-аналитику (приоритизированную) или None — тогда фронт показывает
    сами сигналы."""
    signals = (sig or {}).get("signals") or []
    if not signals:
        return None
    lines, ov_line = signal_lines(sig)
    user = (
        "СИГНАЛЫ НЕДЕЛИ (числа точные, не меняй):\n" + "\n".join(lines) + f"\n{ov_line}\n\n"
        + context + "\n\n"
        + _brief_format()
    )
    try:
        from ..digest.writer import _chat, brief_items
        md, _ti, _to = await _chat(insight_model(), _ANOM_SYSTEM, user, max_tokens=5000)
        return brief_items(md)
    except Exception as e:  # noqa: BLE001
        log.warning("reviews_llm.anomaly_brief упал: %s", e)
        return None


# ── Разбор аудит-дела ───────────────────────────────────────────────────────
_CASE_SYSTEM = (
    "Ты — старший аудитор службы внутреннего аудита банка. Тебе дают аудит-дело: "
    "подборку жалоб клиентов с разметкой и документов, которые собрал коллега. "
    "Пиши только то, что следует из материалов; на каждый вывод — ссылка на номер "
    "материала [N]. Числа бери только из блока «Сводка» — он посчитан программой, "
    "свои не придумывай. Не утверждай, что нарушение было: говори о признаках. "
    "Деловой русский, без вступлений и общих слов."
)
_RISK_RU = {"compliance": "комплаенс", "conduct": "практики", "ops": "операции"}


def case_digest(case: dict) -> tuple[str, str]:
    """Сводка по делу, посчитанная кодом, и перечень материалов для модели."""
    from collections import Counter
    items = case.get("items") or []
    revs = [it["review"] for it in items if it.get("review")]
    lines = [f"Материалов: {len(items)}, из них жалоб: {len(revs)}, "
             f"документов: {sum(1 for it in items if it['kind'] == 'document')}."]
    if revs:
        dates = sorted(r["date"] for r in revs if r.get("date"))
        if dates:
            lines.append(f"Период жалоб: {dates[0]} — {dates[-1]}.")
        for label, cnt in (("Банки", Counter(r["bank"] for r in revs if r.get("bank"))),
                           ("Продукты", Counter(r["product"] for r in revs if r.get("product"))),
                           ("Главные проблемы", Counter(r["issue_label"] for r in revs
                                                        if r.get("issue_label")))):
            if cnt:
                lines.append(f"{label}: " + ", ".join(f"{k} — {v}" for k, v in cnt.most_common(6)) + ".")
        lines.append(
            f"Уже обратились в ЦБ, суд и т. п.: {sum(1 for r in revs if r.get('esc') == 'filed')}; "
            f"грозят: {sum(1 for r in revs if r.get('esc') == 'threat')}; "
            f"уязвимые клиенты: {sum(1 for r in revs if r.get('vulnerable'))}; "
            f"без согласия: {sum(1 for r in revs if r.get('no_consent'))}.")
    listing = []
    for n, it in enumerate(items[:60], 1):
        r = it.get("review")
        if r:
            flags = [x for x in (
                {"filed": "обратился", "threat": "грозит"}.get(r.get("esc") or ""),
                ("уязвимый: " + ", ".join(r["vulnerable"])) if r.get("vulnerable") else None,
                "без согласия" if r.get("no_consent") else None) if x]
            s_ = (f"[{n}] жалоба {r.get('date') or ''} · {r.get('bank') or ''} · "
                  f"{r.get('product') or 'продукт не определён'} · {r.get('issue_label') or ''}"
                  f" ({_RISK_RU.get(r.get('risk') or '', '—')})"
                  + (f" · {'; '.join(flags)}" if flags else "")
                  + f": {r.get('summary') or it.get('title') or ''}")
            if r.get("quote"):
                s_ += f" Цитата: «{r['quote']}»"
        elif it["kind"] == "review":
            s_ = f"[{n}] жалоба (разметки нет): {(it.get('title') or '')[:400]}"
        else:
            s_ = (f"[{n}] документ: {it.get('title') or it.get('url') or ''}"
                  + (f" ({it['bank_name']})" if it.get("bank_name") else ""))
        if it.get("note"):
            s_ += f" Комментарий аудитора: {it['note']}"
        listing.append(s_)
    if len(items) > 60:
        listing.append(f"…показаны 60 материалов из {len(items)}.")
    return "\n".join(lines), "\n".join(listing)


async def case_memo(case: dict) -> str | None:
    """Разбор дела для аудитора: что объединяет материалы, признаки рисков,
    гипотезы о причинах в процессах, что запросить и с чего начать выборку."""
    if not (case or {}).get("items"):
        return None
    summary, listing = case_digest(case)
    user = (
        f"Дело: «{case.get('title')}».\n"
        + (f"Цель, которую записал аудитор: {case['note']}\n" if case.get("note") else "")
        + f"\nСводка (посчитано программой, числа не меняй):\n{summary}\n\n"
        f"Материалы:\n{listing}\n\n"
        "Напиши разбор в Markdown ровно с пятью разделами (### заголовки):\n"
        "### Что объединяет материалы — 2–4 пункта со ссылками [N].\n"
        "### Признаки рисков — какие риски видны (комплаенс, практики, операции), "
        "без утверждения, что нарушение было.\n"
        "### Гипотезы о причинах — что могло сломаться в процессах банка.\n"
        "### Что запросить у подразделения — конкретные документы, выгрузки, регламенты.\n"
        "### С чего начать проверку — 3–5 материалов [N] и почему именно они.\n"
        "Не больше 350 слов."
    )
    try:
        from ..digest.writer import _chat
        md, _ti, _to = await _chat(insight_model(), _CASE_SYSTEM, user, max_tokens=6000)
        return (md or "").strip() or None
    except Exception as e:  # noqa: BLE001 — дело открывается и без разбора
        log.warning("reviews_llm.case_memo упал: %s", e)
        return None
