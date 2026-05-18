from __future__ import annotations
import os, yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]

@dataclass
class Settings:
    database_url: str
    workspace_dir: Path
    raw_dir: Path
    reports_dir: Path
    logs_dir: Path
    browser_profile: str | None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Settings":
        with (ROOT / "config" / "settings.yaml").open() as f:
            cfg = yaml.safe_load(f)
        ws = Path(os.getenv("WORKSPACE_DIR", cfg["workspace_dir"])).resolve()
        return cls(
            database_url=os.environ["DATABASE_URL"],
            workspace_dir=ws,
            raw_dir=ws / "raw",
            reports_dir=ws / "reports",
            logs_dir=ws / "logs",
            browser_profile=os.getenv("OPENCLAW_BROWSER_PROFILE"),
            raw=cfg,
        )

def load_sources() -> dict[str, Any]:
    with (ROOT / "config" / "sources.yaml").open() as f:
        cfg = yaml.safe_load(f)

    # Авто-расширение targets для review-источников: подмешиваем топ-N банков
    # из БД (banki_ratings → product_terms.rate_kind='avg_grade'). Если БД
    # пустая или ошибка — игнорируем, остаются хардкод-targets из yaml.
    try:
        cfg = _expand_review_targets(cfg)
    except Exception as e:
        # Не валим загрузку конфига если БД ещё не готова
        import logging as _l
        _l.getLogger(__name__).info("auto-review-targets skipped: %s", e)
    return cfg


# Маппинг наш-slug (как в BANK_ALIASES) → URL-slug на banki.ru / sravni.ru.
# Ключ должен ТОЧНО совпадать со значением в BANK_ALIASES (нормализованный slug).
# Если slug отсутствует здесь — auto-target пропускается (см. _expand_review_targets).
# Каждый banki-slug проверен curl'ом: 200 + есть review-links на странице.
_BANK_SLUG_OVERRIDES: dict[str, dict[str, str]] = {
    # ── Топ-10 — проверены живым curl ──────────────────────────────────────
    "sberbank":   {"banki": "sberbank",   "sravni": "sberbank"},
    "vtb":        {"banki": "vtb",        "sravni": "vtb"},
    "tinkoff":    {"banki": "tcs",        "sravni": "tinkoff"},
    "alfabank":   {"banki": "alfabank",   "sravni": "alfa-bank"},
    "sovcombank": {"banki": "sovcombank", "sravni": "sovkombank"},
    "rshb":       {"banki": "rshb",       "sravni": "rosselhozbank"},
    "gazprombank":{"banki": "gazprombank","sravni": "gazprombank"},
    "pochtabank": {"banki": "pochtabank", "sravni": "pochta-bank"},
    "raiffeisen": {"banki": "raiffeisen", "sravni": "raiffeisenbank"},
    "mkb":        {"banki": "mkb",        "sravni": "mkb"},
    "akbars":     {"banki": "akbars",     "sravni": "ak-bars"},
    "yandexbank": {"banki": "yandexbank", "sravni": "yandex-bank"},
    "mtsbank":    {"banki": "mts-bank",   "sravni": "mts-bank"},  # ← раньше было mtsbank → 404
    # ── Среднего размера ──────────────────────────────────────────────────
    "otkritie":   {"banki": "otkritie",   "sravni": "otkritie"},
    "uralsib":    {"banki": "uralsib",    "sravni": "uralsib"},
    "rosbank":    {"banki": "rosbank",    "sravni": "rosbank"},
    "bspb":       {"banki": "bspb",       "sravni": "bspb"},
    "psb":        {"banki": "psb",        "sravni": "promsvyazbank"},
    "rencredit":  {"banki": "rencredit",  "sravni": "renessans-credit"},
    "rsb":        {"banki": "rsb",        "sravni": "russkiystandart"},
    "absolut":    {"banki": "absolut",    "sravni": "absolutbank"},
    "lokobank":   {"banki": "lokobank",   "sravni": "loko-bank"},
    "ozonbank":   {"banki": "ozon",       "sravni": "ozon-bank"},  # banki: ozon, не ozonbank
    "domrf":      {"banki": "dom-rf",     "sravni": "dom-rf"},
    "sinara":     {"banki": "sinara",     "sravni": "sinara"},
    "unicredit":  {"banki": "unicredit",  "sravni": "unicreditbank"},
    "homecredit": {"banki": "home-credit","sravni": "home-credit-bank"},
    "norvikbank": {"banki": "norvik",     "sravni": "norvik"},
}


def _expand_review_targets(cfg: dict, top_n: int = 30) -> dict:
    """Расширяет cfg['banki_reviews'].targets и cfg['sravni_reviews'].targets
    топ-N банками из БД. Дедупликация по 'name' таргета.
    Если slug в нашей БД не из _BANK_SLUG_OVERRIDES — пропускаем (рискованно
    угадывать URL без подтверждения)."""
    # Импорт DB ленивый — config.py подгружается раньше db.init()
    try:
        from . import db
        from sqlalchemy import text as _t
        with db.session() as s:
            rows = s.execute(_t("""
                SELECT b.slug, b.name,
                       COALESCE((t.raw->>'total_reviews')::int, 0) AS tr
                  FROM bank b
                  LEFT JOIN product_offer o ON o.bank_id=b.bank_id AND o.category='other'
                  LEFT JOIN product_terms t ON t.offer_id=o.offer_id
                                            AND t.valid_to IS NULL AND t.rate_kind='avg_grade'
                 WHERE b.slug IS NOT NULL AND b.slug NOT LIKE 'unknown_%'
                   AND COALESCE((t.raw->>'total_reviews')::int, 0) > 0
                 ORDER BY tr DESC
                 LIMIT :n
            """), {"n": top_n}).mappings().all()
    except Exception:
        return cfg

    if not rows:
        return cfg

    def _existing_names(src_cfg: dict) -> set[str]:
        return {t.get("name") for t in (src_cfg.get("targets") or []) if t.get("name")}

    banki   = cfg.get("banki_reviews")   or {}
    sravni  = cfg.get("sravni_reviews")  or {}
    banki_targets   = banki.get("targets")  or []
    sravni_targets  = sravni.get("targets") or []
    banki_names  = _existing_names(banki)
    sravni_names = _existing_names(sravni)

    added_b = added_s = 0
    for r in rows:
        slug = r["slug"]
        ov   = _BANK_SLUG_OVERRIDES.get(slug)
        if not ov:
            # Без явного override slug-as-is часто даёт 404 — пропускаем.
            # Это лучше чем создавать "битый" таргет, который будет молча падать.
            continue

        tgt_name = f"{slug}_reviews"
        if banki and tgt_name not in banki_names:
            banki_targets.append({
                "name": tgt_name,
                "url": f"https://www.banki.ru/services/responses/bank/{ov['banki']}/",
                "bank_slug": slug,
                "max_pages": 5,           # 5 страниц = ~40 отзывов/банк, безопасный rate
            })
            banki_names.add(tgt_name); added_b += 1
        if sravni and tgt_name not in sravni_names:
            sravni_targets.append({
                "name": tgt_name,
                "url": f"https://www.sravni.ru/bank/{ov['sravni']}/otzyvy/",
                "bank_slug": slug,
            })
            sravni_names.add(tgt_name); added_s += 1

    if banki:
        banki["targets"]  = banki_targets
        cfg["banki_reviews"] = banki
    if sravni:
        sravni["targets"] = sravni_targets
        cfg["sravni_reviews"] = sravni
    import logging as _l
    _l.getLogger(__name__).info("auto-review-targets: +%s banki, +%s sravni",
                                added_b, added_s)
    return cfg
