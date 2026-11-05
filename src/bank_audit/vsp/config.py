"""Настройки сборщика — из переменных окружения (префикс VSP_), с безопасными
значениями по умолчанию. Каждый источник настраивается отдельно: частота
запросов, параллельность, включён ли он вообще."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class SourceSettings:
    enabled: bool = True
    rps: float = 1.0                 # запросов в секунду к хосту источника
    concurrency: int = 2             # одновременно обрабатываемых отделений
    timeout_s: float = 30.0
    max_retries: int = 4
    page_size: int = 50
    max_pages: int = 1000            # предохранитель от бесконечной пагинации


@dataclass
class Schedule:
    """Расписание дописывания (sync.schedule_next, mass.daemon).

    Отделение проверяется «только новыми» тем чаще, чем чаще у него появляются отзывы:
    интервал = due_target / (отзывов в день за rate_window_d), в пределах [due_min_h, due_max_h].
    Активное отделение — до 4 раз в день, тихое — раз в двое суток. Полный проход (правки,
    ответы банка, пропавшие отзывы) — раз в full_every_d дней с разбросом по отделениям."""
    due_target: float = field(default_factory=lambda: _f("VSP_DUE_TARGET", 1.0))
    due_min_h: float = field(default_factory=lambda: _f("VSP_DUE_MIN_H", 6))
    due_max_h: float = field(default_factory=lambda: _f("VSP_DUE_MAX_H", 48))
    rate_window_d: int = field(default_factory=lambda: _i("VSP_RATE_WINDOW_D", 90))
    full_every_d: float = field(default_factory=lambda: _f("VSP_FULL_EVERY_D", 30))
    retry_h: float = field(default_factory=lambda: _f("VSP_RETRY_H", 6))
    # демон
    sleep_s: float = field(default_factory=lambda: _f("VSP_DAEMON_SLEEP_S", 600))
    batch: int = field(default_factory=lambda: _i("VSP_DAEMON_BATCH", 200))
    registry_every_h: float = field(default_factory=lambda: _f("VSP_REGISTRY_EVERY_H", 24))
    discover_every_d: float = field(default_factory=lambda: _f("VSP_DISCOVER_EVERY_D", 30))
    match_every_h: float = field(default_factory=lambda: _f("VSP_MATCH_EVERY_H", 6))
    banksite_every_d: float = field(default_factory=lambda: _f("VSP_BANK_SITE_EVERY_D", 30))
    pause_on_block_h: float = field(default_factory=lambda: _f("VSP_PAUSE_ON_BLOCK_H", 3))
    backoff_factor: float = field(default_factory=lambda: _f("VSP_BACKOFF_FACTOR", 0.7))
    min_rps: float = field(default_factory=lambda: _f("VSP_MIN_RPS", 0.1))


@dataclass
class Settings:
    db_url: str = field(default_factory=lambda: os.getenv(
        "VSP_DB_URL", "sqlite:///workspace/vsp/vsp_reviews.sqlite"))
    author_salt: str = field(default_factory=lambda: os.getenv("VSP_AUTHOR_SALT", "change-me"))
    user_agent: str = field(default_factory=lambda: os.getenv(
        "VSP_USER_AGENT",
        # актуальная версия Chrome: с устаревшей 2gis.ru уводит на заглушку /museum
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"))
    log_dir: str = field(default_factory=lambda: os.getenv("VSP_LOG_DIR", "workspace/vsp/logs"))
    # сайт банка для паспорта ВСП (форматы, часы, услуги); пусто — шаг выключен
    bank_site: str = field(default_factory=lambda: os.getenv("VSP_BANK_SITE", ""))
    bank_site_rps: float = field(default_factory=lambda: _f("VSP_BANK_SITE_RPS", 0.5))
    sources: dict[str, SourceSettings] = field(default_factory=dict)
    schedule: Schedule = field(default_factory=Schedule)

    def source(self, name: str) -> SourceSettings:
        return self.sources.setdefault(name, SourceSettings())

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        defaults = {
            # 2ГИС — публичный JSON-сервис отзывов: умеренно
            "2gis": SourceSettings(rps=_f("VSP_2GIS_RPS", 1.0), concurrency=_i("VSP_2GIS_CONC", 2),
                                   page_size=_i("VSP_2GIS_PAGE", 50)),
            # Яндекс Карты — страницы организаций: медленно, без параллельности
            "yandex": SourceSettings(rps=_f("VSP_YANDEX_RPS", 0.3), concurrency=_i("VSP_YANDEX_CONC", 1),
                                     page_size=_i("VSP_YANDEX_PAGE", 50)),
            "domclick": SourceSettings(enabled=os.getenv("VSP_DOMCLICK", "0") == "1",
                                       rps=_f("VSP_DOMCLICK_RPS", 0.5), concurrency=1),
        }
        for name, st in defaults.items():
            st.enabled = os.getenv(f"VSP_{name.upper()}_ENABLED", "1" if st.enabled else "0") == "1"
            s.sources[name] = st
        return s
