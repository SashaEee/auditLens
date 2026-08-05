"""Finuslugi.ru reviews — JSON API маркетплейса Мосбиржи.

Второй источник жалоб рядом с корпусом banki.ru, на котором стоит вкладка
«Отзывы». Другая площадка — другая аудитория и другой состав жалоб.

Сохраняем ТОЛЬКО 1-2 звезды: вкладка это риск-радар, и корпус banki.ru рядом
собран по тому же правилу. Хвалебные отзывы в общей ленте с жалобами разбавляют
выдачу и аудитору ничего не дают. Пролистывать их при сборе всё равно
приходится — лента отдаёт все оценки подряд, негатив лежит вперемешку.

    GET /money_data/Reviews.json?type=BANK&company={id}&sort=created&order=DESC
                                &page=N&limit=100
      → {"result": {"reviews": [{id, company_id, service_id, url, title,
                                 date, name, rating, review, ...}]}}
    GET /money_data/Root.json
      → {"companies": [{id, name, url}]}   — справочник, 364 банка

⚠️ Собирать ОБЯЗАТЕЛЬНО побанково, через `company={id}`. Общая лента без этого
параметра выглядит удобнее (один запрос на все банки), но она перекошена до
бесполезности: на выборке в 3000 отзывов две трети занимал ОДИН банк, а Сбера
там было 9 штук. По ней же получалось, что 89 отзывов из 100 — пятизвёздочные,
и напрашивался вывод, что площадка модерирует негатив. Это неверно: у Сбера
поимённо 675 отзывов, из них 578 однозвёздочных. Перекос давала не площадка, а
один банк, заливавший ленту свежими положительными отзывами.

Параметр `company` в документации не описан и подбирается опытом: `company_id`,
`companyId` и `bank_id` молча игнорируются и возвращают общую ленту — то есть
ошибка в имени параметра выглядит как рабочий сбор.

Замер с прод-сервера: 53 мс на страницу, обычный HTTP, ни капчи, ни подмены
TLS-отпечатка (в отличие от sravni, где приходится выдавать себя за браузер).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

import httpx
from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential)

from ..models import RawSnapshot, ReviewDraft
from .base import FetchResult, SourceAdapter

log = logging.getLogger(__name__)

_API = "https://finuslugi.ru/money_data/Reviews.json"
_ROOT = "https://finuslugi.ru/money_data/Root.json"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
}

_PAGE_LIMIT = 100
_PAUSE_S = 1.0          # между страницами; API быстрый, спешить некуда
_MIN_TEXT = 20
# Берём только негатив: вкладка «Отзывы» — риск-радар, и корпус banki.ru, рядом с
# которым живёт этот источник, тоже собран по 1-2 звёздам. Хвалебные отзывы в
# одной ленте с жалобами разбавляют выдачу и ничего не добавляют аудитору.
_MAX_RATING = float(os.getenv("FINUSLUGI_MAX_RATING", "2"))
# Перекрытие при инкрементальной догрузке: страницу, где начинается уже известное,
# дочитываем целиком — отзывы с одинаковой датой могут разъехаться по страницам.
_OVERLAP_PAGES = 1


def _known_latest(source: str, bank_name: str):
    """Самый свежий отзыв этого банка, который у нас уже есть.

    Нужен, чтобы ночной прогон не выкачивал всю историю заново: лента
    отсортирована по дате убыванию, поэтому дойдя до известного можно
    останавливаться. Первый прогон (ничего не знаем) идёт до конца — это и есть
    обещанный бэкфилл «всё до сегодняшнего дня».
    """
    try:
        from .. import db
        from sqlalchemy import text as _t
        with db.session() as s:
            return s.execute(_t("""
                SELECT max(r.posted_at) FROM review r
                JOIN bank b USING (bank_id)
                WHERE r.source = :src AND b.name = :bn
            """), {"src": source, "bn": bank_name}).scalar()
    except Exception as e:      # первый запуск, нет таблицы, нет коннекта
        log.debug("finuslugi: последняя известная дата не определена (%s)", e)
        return None


def _norm_name(s: str) -> str:
    """Имя банка к сопоставимому виду: у площадки «АЛЬФА БАНК», у нас
    «Альфа-Банк» — различаются регистром, дефисом и кавычками."""
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[«»\"'`]", " ", s)
    s = re.sub(r"[-–—]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _match_company(companies: dict[str, dict], want: str) -> str | None:
    target = _norm_name(want)
    if not target:
        return None
    exact = [cid for cid, c in companies.items() if _norm_name(c.get("name")) == target]
    if exact:
        return min(exact, key=int)      # при дублях берём меньший id — он старше
    # запасной проход: имя площадки содержит наше целиком («Сбербанк России»)
    part = [cid for cid, c in companies.items()
            if target in _norm_name(c.get("name")).split(" (")[0]]
    return min(part, key=int) if part else None


def _parse_date(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        # у части отзывов дата — заглушка 1971-01-01; это не ошибка разбора, а
        # дырка в данных площадки, и в трендах она даёт ложный хвост
        return dt if dt.year >= 2000 else None
    return None


def _page_is_older(chunk: list[dict], latest) -> bool:
    """Вся страница старше того, что у нас уже есть."""
    dates = [d for d in (_parse_date(r.get("date")) for r in chunk) if d]
    if not dates:
        return False
    ref = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
    return max(dates) <= ref


class FinuslugiReviewsAdapter(SourceAdapter):
    name = "finuslugi_reviews"

    # ── сбор ─────────────────────────────────────────────────────────────
    def fetch(self, target: dict[str, Any]) -> FetchResult:
        max_pages = int(target.get("max_pages") or 3)
        want = (target.get("bank_name") or "").strip()
        company_id = target.get("company_id")

        @retry(stop=stop_after_attempt(3),
               wait=wait_exponential(multiplier=2, min=3, max=20),
               retry=retry_if_exception_type((httpx.TransportError,
                                              httpx.TimeoutException)))
        def _get(client: httpx.Client, url: str, params: dict | None = None):
            return client.get(url, params=params)

        reviews: list[dict] = []
        with httpx.Client(headers=_HEADERS, follow_redirects=True,
                          timeout=httpx.Timeout(connect=8.0, read=25.0,
                                                write=8.0, pool=8.0)) as client:
            try:
                root = _get(client, _ROOT)
            except Exception as e:
                raise RuntimeError(f"finuslugi: справочник банков недоступен "
                                   f"({type(e).__name__}: {str(e)[:100]})")
            if root.status_code != 200:
                raise RuntimeError(f"finuslugi: Root.json HTTP {root.status_code}")
            try:
                companies = {str(c.get("id")): {"name": c.get("name") or "",
                                                "url": c.get("url") or ""}
                             for c in root.json().get("companies", [])}
            except Exception as e:
                raise RuntimeError(f"finuslugi: справочник не разобрался ({e})")
            if not companies:
                raise RuntimeError("finuslugi: справочник банков пуст")

            # Банк задаётся именем — id у площадки свой и в нашей БД его нет.
            # Сверяем по нормализованному имени, чтобы «Альфа-Банк» нашёлся и
            # как «АЛЬФА БАНК».
            if company_id is None and want:
                company_id = _match_company(companies, want)
                if company_id is None:
                    raise RuntimeError(
                        f"finuslugi: банк {want!r} не найден в справочнике "
                        f"({len(companies)} записей)")
            if company_id is None:
                raise RuntimeError(
                    "finuslugi: в таргете нет ни company_id, ни bank_name. "
                    "Собирать общей лентой нельзя — она перекошена одним банком")

            latest = _known_latest(self.name, want) if want else None
            stale_since = None
            for page in range(1, max_pages + 1):
                params = {"type": "BANK", "sort": "created", "order": "DESC",
                          "company": company_id,
                          "page": page, "limit": _PAGE_LIMIT}
                try:
                    resp = _get(client, _API, params)
                except Exception as e:
                    raise RuntimeError(f"finuslugi: страница {page} — "
                                       f"{type(e).__name__}: {str(e)[:100]}")
                if resp.status_code != 200:
                    raise RuntimeError(f"finuslugi: страница {page} HTTP {resp.status_code}")
                chunk = (resp.json().get("result") or {}).get("reviews") or []
                if not chunk:
                    break               # лента кончилась раньше max_pages
                reviews.extend(chunk)
                # Дошли до уже известного — дальше вся история, её мы забрали
                # на первом прогоне. Дочитываем ещё страницу: отзывы одной даты
                # могут оказаться по обе стороны границы.
                if latest is not None and _page_is_older(chunk, latest):
                    if stale_since is None:
                        stale_since = page
                    if page - stale_since >= _OVERLAP_PAGES:
                        log.info("finuslugi %s: дошли до известного на стр. %d — "
                                 "дальше не идём", want or company_id, page)
                        break
                if page < max_pages:
                    time.sleep(_PAUSE_S)

        if not reviews:
            raise RuntimeError("finuslugi: лента вернула ноль отзывов")
        # Страховка от молчаливой подмены: неверно названный параметр фильтра
        # площадка игнорирует и отдаёт общую ленту — сбор «работает», а данные
        # не того банка. Дешевле проверить, чем потом искать причину в БД.
        alien = {str(r.get("company_id")) for r in reviews} - {str(company_id)}
        if alien:
            raise RuntimeError(
                f"finuslugi: фильтр по банку не сработал — в выдаче чужие "
                f"company_id {sorted(alien)[:5]}, ожидался {company_id}")

        # В снимок кладём только те банки, что встретились: справочник целиком
        # весит мегабайт, и хранить его копию в каждом снимке незачем.
        used = {str(r.get("company_id")) for r in reviews}
        payload = json.dumps(
            {"companies": {k: v for k, v in companies.items() if k in used},
             "reviews": reviews},
            ensure_ascii=False).encode("utf-8")

        path, digest, n = self.raw.write(
            self.name, target["name"], payload, "json",
            meta={"url": _API, "target": target["name"], "pages": max_pages,
                  "reviews": len(reviews)},
        )
        snap = RawSnapshot(
            source=self.name, target_name=target["name"], url=_API,
            fetched_at=datetime.now(timezone.utc), http_status=200,
            content_sha256=digest, storage_path=path, bytes=n,
        )
        log.info("finuslugi_reviews: собрано %d отзывов со %d страниц, банков %d",
                 len(reviews), max_pages, len(used))
        return FetchResult(snapshot=snap, html=payload)

    # ── разбор ───────────────────────────────────────────────────────────
    def parse_reviews(self, html: bytes,
                      target: dict[str, Any]) -> Iterable[ReviewDraft]:
        try:
            data = json.loads(html.decode("utf-8", errors="ignore"))
        except Exception as e:
            log.warning("finuslugi_reviews: снимок не разобрался (%s)", e)
            return
        companies: dict = data.get("companies") or {}
        n_ok = n_short = n_pos = n_nobank = 0

        for r in data.get("reviews") or []:
            body = (r.get("review") or "").strip()
            if len(body) < _MIN_TEXT:
                n_short += 1
                continue

            # Хвалебные отзывы не сохраняем. Отсекаем здесь, а не при сборе:
            # лента отдаёт все оценки подряд, и пролистать их всё равно надо,
            # чтобы добраться до негатива на следующих страницах.
            try:
                rv = float(r.get("rating")) if r.get("rating") is not None else None
            except (TypeError, ValueError):
                rv = None
            # ноль означает «без оценки», а не «очень плохо» — такие пропускаем
            if rv is None or not (1 <= rv <= _MAX_RATING):
                n_pos += 1
                continue
            bank = companies.get(str(r.get("company_id"))) or {}
            bank_name = (bank.get("name") or "").strip()
            if not bank_name:
                # без имени банка отзыв некуда отнести — резолвер его отбросит,
                # а строка-сирота в базе только мешает считать доли
                n_nobank += 1
                continue

            posted = _parse_date(r.get("date"))

            rating = Decimal(str(rv))

            slug = bank.get("url") or ""
            rid = r.get("url") or r.get("id")
            url = (f"https://finuslugi.ru/banki/{slug}/otzyvy/{rid}"
                   if slug and rid else "https://finuslugi.ru/banki")

            n_ok += 1
            yield ReviewDraft(
                source=self.name,
                # id у источника свой и стабильный — самодельный хэш не нужен
                source_review_id=str(r.get("id") or rid),
                source_url=url,
                bank_name_raw=bank_name,
                posted_at=posted,
                rating=rating,
                title=(r.get("title") or "").strip() or None,
                text=body,
                author_raw=(r.get("name") or "").strip() or None,
                # service_id кладём как есть: сопоставлять его с внутренними
                # категориями продуктов пришлось бы руками поддерживаемым
                # словарём, а он устареет быстрее, чем появится польза
                raw={"service_id": r.get("service_id"),
                     "region_id": r.get("region_id"),
                     "answers_count": r.get("answers_count")},
            )
        log.info("finuslugi_reviews: взято %d (1-%g звёзд), отброшено: "
                 "хвалебных %d, коротких %d, без банка %d",
                 n_ok, _MAX_RATING, n_pos, n_short, n_nobank)
