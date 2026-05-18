"""HTML parser: чистка nav/footer/scripts + сохранение структуры заголовков.

Selectolax быстрее чем BeautifulSoup. Удаляем шум:
  • <script>, <style>, <noscript>, <svg>
  • <nav>, <footer>, <header> (часто)
  • Элементы с классами/id navigation/menu/cookie/banner/share

ОТДЕЛЬНО извлекаем JSON-LD schema.org разметку (`<script type="application/ld+json">`)
для типов Review/Product/Article — на сайтах вроде banki.ru сами отзывы лежат
именно там, а не в видимом DOM.

Результат: markdown-style текст с # заголовками для chunker'а.
"""
from __future__ import annotations
import json
import re
from selectolax.parser import HTMLParser

from .base import ParsedDoc


def _extract_jsonld_reviews(tree: HTMLParser) -> list[str]:
    """Извлекает текст из <script type="application/ld+json"> с типом Review.
    Возвращает список текстовых блоков отдельно (для последующего объединения)."""
    out: list[str] = []
    for s in tree.css('script[type="application/ld+json"]'):
        raw = s.text(strip=False) or ""
        if not raw or len(raw) < 50:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        # JSON-LD может быть массивом или объектом
        items = data if isinstance(data, list) else [data]
        # Также может быть объект с @graph
        flat = []
        for it in items:
            if isinstance(it, dict):
                if isinstance(it.get("@graph"), list):
                    flat.extend(it["@graph"])
                else:
                    flat.append(it)
        # Раскрываем nested review-arrays: Organization.review[], Product.review[]
        expanded: list[dict] = []
        for it in flat:
            if not isinstance(it, dict):
                continue
            expanded.append(it)
            # У Organization/Product/LocalBusiness есть array `review`
            for rev_field in ("review", "reviews"):
                inner = it.get(rev_field)
                if isinstance(inner, list):
                    for r in inner:
                        if isinstance(r, dict):
                            # Помечаем @type если не задан
                            if "@type" not in r:
                                r = {**r, "@type": "Review"}
                            expanded.append(r)
            # AggregateRating часто nested
            agg = it.get("aggregateRating")
            if isinstance(agg, dict) and "@type" not in agg:
                expanded.append({**agg, "@type": "AggregateRating",
                                  "_parent_name": it.get("name", "")})

        for it in expanded:
            if not isinstance(it, dict):
                continue
            t = it.get("@type", "")
            t_str = str(t)
            # Review entity
            if "Review" in t_str:
                name = it.get("name") or ""
                body = it.get("reviewBody") or it.get("description") or ""
                rating = it.get("reviewRating") or {}
                if isinstance(rating, dict):
                    rate_val = rating.get("ratingValue") or ""
                else:
                    rate_val = ""
                author = it.get("author") or {}
                if isinstance(author, dict):
                    author_name = author.get("name", "")
                else:
                    author_name = str(author)
                date = it.get("datePublished") or ""
                # Собираем структурированный текст отзыва
                pieces = []
                if name: pieces.append(f"## {name}")
                meta_parts = []
                if rate_val: meta_parts.append(f"Оценка: {rate_val}/5")
                if author_name: meta_parts.append(f"Автор: {author_name}")
                if date: meta_parts.append(f"Дата: {date}")
                if meta_parts: pieces.append(" · ".join(meta_parts))
                if body: pieces.append(body)
                if pieces and (body or rate_val):
                    out.append("\n".join(pieces))
            # AggregateRating
            elif "AggregateRating" in t_str:
                rate = it.get("ratingValue") or ""
                count = it.get("reviewCount") or it.get("ratingCount") or ""
                parent = it.get("_parent_name", "")
                if rate or count:
                    pref = f"{parent}: " if parent else ""
                    out.append(f"{pref}Общий рейтинг: {rate}/5 (отзывов: {count})")
    return out

# Селекторы шума — удаляются из дерева до извлечения текста
_NOISE_SELECTORS = [
    "script", "style", "noscript", "svg", "iframe",
    "nav", "footer", "header.site-header", "[role=navigation]",
    "[role=banner]", "[role=contentinfo]",
    ".nav", ".navigation", ".menu", ".breadcrumb",
    ".cookie", ".cookie-banner", ".cookies-banner",
    ".social", ".share", ".social-share",
    ".sidebar", ".widget", ".popup", ".modal",
    ".advertising", ".banner", ".promo-banner",
    ".comments", ".related", ".recommended",
    "[id*=cookie]", "[class*=cookie-]",
]
_BLOCKLIKE = {"div", "section", "article", "main", "aside", "li", "td"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def parse_html(content: bytes, url: str = "") -> ParsedDoc:
    text = content.decode("utf-8", errors="ignore")
    tree = HTMLParser(text)

    # Title
    title = None
    title_node = tree.css_first("title")
    if title_node:
        title = (title_node.text() or "").strip() or None
        # Часто title содержит и название сайта — берём только до « | »
        if title:
            title = re.split(r"[|—–]", title, 1)[0].strip()

    # ВАЖНО: извлекаем JSON-LD ДО удаления шума (script удаляется в _NOISE_SELECTORS)
    jsonld_reviews = _extract_jsonld_reviews(tree)

    # Удаляем шум
    for sel in _NOISE_SELECTORS:
        for node in tree.css(sel):
            try:
                node.decompose()
            except Exception:
                pass

    # Главный контейнер контента — пробуем main → article → body
    root = (tree.css_first("main")
            or tree.css_first("article")
            or tree.css_first("[role=main]")
            or tree.body)
    if root is None:
        return ParsedDoc(doc_type="html", title=title)

    # Простая и надёжная стратегия:
    # 1. Сначала в порядке появления собираем заголовки и блоки текста
    #    через CSS-селекторы (selectolax поддерживает node.css() с descendant)
    out_lines: list[str] = []

    # Стратегия маркера-плейсхолдера: пройдёмся по всем h1-h6, p, li, blockquote
    # и для каждого извлечём текст. Для сохранения порядка вставим маркеры в DOM,
    # либо просто соберём в порядке итерации tree.css.
    # selectolax garentirует document order для css().
    selectors = "h1,h2,h3,h4,h5,h6,p,li,blockquote,article header,article > div,section > div"
    seen_node_ids = set()
    for el in root.css(selectors):
        # Пропускаем дубль если родитель уже взят (li > p) — селектор может дать оба
        nid = id(el)
        if nid in seen_node_ids:
            continue
        seen_node_ids.add(nid)
        tag = (el.tag or "").lower()
        text = (el.text(separator=" ") or "").strip()
        text = re.sub(r"\s+", " ", text)
        if not text or len(text) < 3:
            continue
        if tag in _HEADING_TAGS:
            level = int(tag[1])
            out_lines.append("\n" + "#" * level + " " + text + "\n")
        elif tag == "li":
            out_lines.append("- " + text)
        elif tag in ("p", "blockquote"):
            out_lines.append("\n" + text + "\n")
        else:
            # div/section/header — рискованно, может дублироваться. Берём только
            # если текст разумной длины и без вложенных p/h*
            if len(text) > 50 and len(text) < 1500 and not el.css_first("p,h1,h2,h3,h4,h5,h6,li"):
                out_lines.append("\n" + text + "\n")

    body = "\n".join(out_lines)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    # Fallback: если всё равно пусто — берём весь текст root (грубый, но рабочий)
    if len(body) < 200:
        fallback = (root.text(separator="\n", strip=True) or "").strip()
        fallback = re.sub(r"\n{3,}", "\n\n", fallback)
        if len(fallback) > len(body):
            body = fallback

    # Дописываем JSON-LD reviews (отзывы клиентов — самое ценное на banki.ru)
    if jsonld_reviews:
        body = body + "\n\n# Отзывы клиентов\n\n" + "\n\n---\n\n".join(jsonld_reviews)

    return ParsedDoc(
        title=title, text=body, doc_type="html",
        meta={"url": url, "char_count": len(body),
              "jsonld_reviews": len(jsonld_reviews)},
    )
