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
    # Элементы управления каталогом. Именно они порождают «По сумме: от 100000
    # рублей / от 200000 рублей…» и «- 1 месяц - 2 месяца…»: числа оттуда
    # структурно неотличимы от условий продукта и создают ложные факты.
    "form", "select", "option", "label", "fieldset",
    "[class*=filter]", "[class*=facet]", "[class*=chips]", "[class*=tabs]",
    "[role=tablist]", "[role=search]", "[class*=range-]", "[class*=slider]",
    "[aria-hidden=true]",
]

# Числовая единица: строка с ней несёт условие продукта и НИКОГДА не режется
# правилами длины/плотности — иначе «Ставка 16,5% годовых» (24 символа)
# исчезнет вместе с меню.
_UNIT_RE = re.compile(r"\d[\d\s\u00a0]*(?:[.,]\d+)?\s*(?:%|₽|руб|коп|мес|год|лет|дн|шт)",
                      re.IGNORECASE)
_MIN_LINE = 40          # ниже этой длины строка без смысла считается элементом UI

# Короткие строки отсеиваются СТРУКТУРНО, без словарей и без знания предметной
# области. Работают два независимых признака вёрстки:
#   1) однородная группа: элемент — один из нескольких одинаковых по тегу
#      соседей, и все они короткие. Так выглядят меню, чипы, списки фильтров,
#      варианты <select>. Одиночная короткая строка среди длинных абзацев так
#      не выглядит — и остаётся в тексте как содержание;
#   2) длинная серия подряд: даже в плоской вёрстке из <div> десяток коротких
#      строк подряд — это интерфейс, а не проза.
# Оба признака одинаково работают для вопроса про ставку и про порядок
# оформления карты: они смотрят на форму документа, а не на слова в нём.
_UI_RUN_MIN = 6          # столько коротких строк подряд считаем серией UI
_UI_GROUP_MIN = 2        # столько одинаковых коротких соседей считаем группой


def _is_bare_label(text: str) -> bool:
    """Голая метка-ярлык («Карты», «Онлайн») против фразы («Нужен паспорт»).

    Меню и чипы называют РАЗДЕЛ одним словом; содержание всегда высказывание
    хотя бы из двух слов. Признак не знает предметной области и не зависит от
    языка — он про число слов, а не про то, какие это слова.
    """
    return len(text.split()) < 2


def _in_short_group(el) -> bool:
    """Элемент — часть однородной группы коротких соседей (меню, чипы, список)."""
    parent = el.parent
    if parent is None:
        return False
    tag = el.tag
    peers = [c for c in parent.iter() if c.tag == tag]
    if len(peers) < _UI_GROUP_MIN:
        return False
    for peer in peers:
        txt = (peer.text(separator=" ") or "").strip()
        if len(txt) >= _MIN_LINE:
            return False        # среди соседей есть длинный — это не группа UI
    return True


def _flush_short_run(run: list[str], out_lines: list[str],
                     ui_lines: list[str]) -> None:
    """Сбрасывает накопленные короткие строки по ДЛИНЕ СЕРИИ (признак 2)."""
    if not run:
        return
    if len(run) >= _UI_RUN_MIN:
        ui_lines.extend(run)
    else:
        out_lines.extend(run)
    run.clear()


def _link_density(node) -> float:
    """Доля текста внутри ссылок. У меню и списков категорий ≈1, у абзаца ≈0."""
    try:
        total = len((node.text(separator=" ") or "").strip())
        if total < 20:
            return 0.0
        inside = sum(len((a.text(separator=" ") or "").strip())
                     for a in node.css("a"))
        return min(1.0, inside / total)
    except Exception:
        return 0.0
_BLOCKLIKE = {"div", "section", "article", "main", "aside", "li", "td"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _tables_to_markdown(root) -> list[str]:
    """<table> → markdown. Самые доказательные данные страницы (тарифные
    сетки, лимиты, ПСК по категориям) живут в таблицах, а извлечение по
    селекторам p/li/h* рассыпало их в кашу без строк и колонок — агент брал
    число из рекламного абзаца «ставка от…» вместо ячейки таблицы."""
    out: list[str] = []
    for tbl in root.css("table"):
        rows = []
        for tr in tbl.css("tr")[:40]:
            cells = [re.sub(r"\s+", " ", (c.text(separator=" ") or "").strip())
                     for c in tr.css("th,td")[:12]]
            if any(cells):
                rows.append(cells)
        # Отсекаем layout-таблицы: смысловая имеет ≥2 строк и ≥2 колонок.
        if len(rows) < 2 or max(len(r) for r in rows) < 2:
            continue
        width = max(len(r) for r in rows)
        norm = [r + [""] * (width - len(r)) for r in rows]
        md = ["| " + " | ".join(norm[0]) + " |",
              "|" + "---|" * width]
        md += ["| " + " | ".join(r) + " |" for r in norm[1:]]
        # Подпись таблицы (caption) — контекст колонок, без неё «23,4» безлика
        cap = tbl.css_first("caption")
        cap_txt = re.sub(r"\s+", " ", (cap.text() or "").strip()) if cap else ""
        out.append((f"**{cap_txt}**\n" if cap_txt else "") + "\n".join(md))
    return out


# Расширения файлов, в которых регуляторы и банки публикуют ПЕРВОИСТОЧНИКИ:
# таблицы значений, тарифные сетки, формы отчётности. Текст страницы-каталога
# их не содержит — там только ссылки.
_FILE_EXT = (".xlsx", ".xls", ".pdf", ".docx", ".doc", ".csv", ".zip", ".xlsm")


def _collect_links(root, base_url: str) -> tuple[list[dict], list[dict]]:
    """Ссылки страницы: (файлы, подразделы того же сайта).

    ЗАЧЕМ. Агент читал страницу-оглавление, не находил ответа и объявлял
    «данных нет», хотя ссылка на нужный файл была прямо в разметке. Текст
    оглавления бесполезен — ценность в ССЫЛКАХ, и их надо отдать агенту,
    чтобы он мог пойти на шаг глубже вместо капитуляции.
    """
    from urllib.parse import urljoin, urlparse
    host = (urlparse(base_url).hostname or "").lower().removeprefix("www.")
    files: list[dict] = []
    sections: list[dict] = []
    seen: set[str] = set()
    for a in root.css("a[href]")[:800]:
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href)
        if not full.startswith("http") or full in seen:
            continue
        seen.add(full)
        anchor = re.sub(r"\s+", " ", (a.text() or "").strip())[:120]
        path = (urlparse(full).path or "").lower()
        if path.endswith(_FILE_EXT):
            files.append({"url": full, "anchor": anchor,
                          "ext": path.rsplit(".", 1)[-1]})
        else:
            h = (urlparse(full).hostname or "").lower().removeprefix("www.")
            if h == host and len(anchor) >= 4:
                sections.append({"url": full, "anchor": anchor})
    return files[:40], sections[:120]


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

    # Удаляем шум. КРИТИЧНО: узел с числом-единицей не удаляем никогда — у
    # части банков условия свёрстаны вкладками и чипами ([class*=tabs],
    # [class*=chips]), и слепое удаление по селектору выбрасывает настоящие
    # ставки вместе с меню. Проверено замером: без этой оговорки digit-recall
    # у Т-Банка падал со 100% до 62%.
    _KEEP_IF_NUMBER = ("form", "select", "label", "fieldset", "[class*=filter]",
                       "[class*=facet]", "[class*=chips]", "[class*=tabs]",
                       "[role=tablist]", "[class*=range-]", "[class*=slider]")
    for sel in _NOISE_SELECTORS:
        guard = sel in _KEEP_IF_NUMBER
        for node in tree.css(sel):
            try:
                if guard and _UNIT_RE.search(node.text(separator=" ") or ""):
                    continue          # внутри есть условие — оставляем блок
                node.decompose()
            except Exception:
                pass

    # Главный контейнер контента. ВАЖНО: выбираем по объёму текста, а не по
    # первому найденному тегу. На реальных страницах банков встречается пустой
    # декоративный <article> (39 символов при 25 000 рядом) — прежний код брал
    # его и выбрасывал всю страницу: из 919 КБ HTML модель получала 40 символов.
    body = tree.body
    body_len = len((body.text(separator=" ") if body else "") or "")
    root, root_len = body, body_len
    for sel in ("main", "article", "[role=main]"):
        cand = tree.css_first(sel)
        if cand is None:
            continue
        cand_len = len((cand.text(separator=" ") or ""))
        # Кандидат должен нести ЗАМЕТНУЮ долю текста страницы, иначе это
        # обёртка-пустышка, а содержимое лежит снаружи.
        if cand_len > root_len or (cand_len >= body_len * 0.4 and cand_len > 500):
            root, root_len = cand, cand_len
            break
    if root is None:
        return ParsedDoc(doc_type="html", title=title)

    # Таблицы: конвертируем в markdown и УДАЛЯЕМ из дерева до основного
    # прохода, чтобы ячейки не дублировались бесструктурной кашей.
    tables_md = _tables_to_markdown(root)
    for tbl in root.css("table"):
        try:
            tbl.decompose()
        except Exception:
            pass

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
    seen_text: set[str] = set()      # дедуп повторяющихся строк
    ui_lines: list[str] = []         # отсеянные элементы интерфейса
    short_run: list[str] = []        # накопитель коротких строк подряд
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
        has_number = bool(_UNIT_RE.search(text))
        # 1) Дедупликация: шапка, хлебные крошки и подписи повторяются десятками
        #    строк и едут в модель на каждом ходу диалога.
        key = text.lower()
        if key in seen_text:
            continue
        seen_text.add(key)
        # 2) Короткая строка без числа — почти всегда пункт меню, чип, ярлык.
        #    Строку с числом не трогаем никогда (защита условий продукта).
        # Короткая строка — признак меню/чипа ТОЛЬКО в списках и ячейках.
        # Абзац (p/blockquote) короткой быть имеет право: «Ставка фиксированная.»
        # — это содержание, а не элемент интерфейса.
        if (len(text) < _MIN_LINE and not has_number
                and tag not in _HEADING_TAGS and tag not in ("p", "blockquote")):
            if _is_bare_label(text) and _in_short_group(el):
                _flush_short_run(short_run, out_lines, ui_lines)
                ui_lines.append(text)
                continue
            # Не группа: решение откладываем до конца серии — одиночная короткая
            # строка среди абзацев это содержание («Нужен паспорт»), а десяток
            # подряд в плоской вёрстке — интерфейс.
            short_run.append(text)
            continue
        if short_run:
            _flush_short_run(short_run, out_lines, ui_lines)
        # 3) Блок, где текст почти целиком в ссылках, — навигация, не контент.
        if not has_number and _link_density(el) > 0.6:
            ui_lines.append(text)
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

    # Карточки продуктов в современных SPA — это div/span со сгенерированными
    # классами (sc-dSCufp autolayout-item), а не p/li/h*. Наш селектор их не
    # видел, и ставки «14,8%», «12,65%» из каталога терялись целиком. Добираем
    # ЛИСТЬЯ (без вложенных блоков), в которых есть число с единицей.
    _flush_short_run(short_run, out_lines, ui_lines)

    # Идём от самых глубоких узлов к внешним: сначала конкретное значение
    # («14,8%»), потом его контейнер. Дедуп по подстроке не даёт продублировать
    # одно и то же значение внутри вложенных блоков.
    _cards = [re.sub(r"\s+", " ", (el.text(separator=" ") or "").strip())
              for el in root.css("span,td,dd,strong,b,div")]
    for text in _cards:
        if not text or len(text) > 300 or not _UNIT_RE.search(text):
            continue
        key = text.lower()
        if key in seen_text:
            continue
        # значение уже вошло в состав более полной, ранее взятой строки
        if any(key in prev for prev in seen_text if len(prev) > len(key)):
            continue
        seen_text.add(key)
        out_lines.append(text)

    body = "\n".join(out_lines)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    # Fallback: если всё равно пусто — берём весь текст root. Раньше он шёл
    # СЫРЫМ, мимо дедупа и отсева, и на бедных страницах модель получала
    # полный дубль меню. Прогоняем через те же правила.
    # Порог 100, а не 200: после дедупликации нормально собранная страница
    # может стать короткой, и прежний порог отправлял её на аварийный путь,
    # где абзацы неотличимы от пунктов меню.
    if len(body) < 100:
        raw = (root.text(separator="\n", strip=True) or "").strip()
        kept, seen_fb = [], set()
        for line in raw.split("\n"):
            line = re.sub(r"\s+", " ", line).strip()
            if not line or line.lower() in seen_fb:
                continue
            seen_fb.add(line.lower())
            if len(line) < _MIN_LINE and not _UNIT_RE.search(line):
                ui_lines.append(line)
                continue
            kept.append(line)
        fallback = "\n".join(kept)
        if len(fallback) > len(body):
            body = fallback

    if tables_md:
        body = body + "\n\n# Таблицы страницы\n\n" + "\n\n".join(tables_md[:12])

    # Отсеянное не пропадает: складываем компактным хвостом с явной пометкой.
    # Так агент видит, что на странице был фильтр по сумме или список городов,
    # но не может принять «от 100 000 ₽» за условие вклада.
    if ui_lines:
        uniq = list(dict.fromkeys(ui_lines))[:40]
        body = body + "\n\n# Элементы интерфейса (не условия продукта)\n" \
             + " · ".join(uniq)

    # Дописываем JSON-LD reviews (отзывы клиентов — самое ценное на banki.ru)
    if jsonld_reviews:
        body = body + "\n\n# Отзывы клиентов\n\n" + "\n\n---\n\n".join(jsonld_reviews)

    files, sections = _collect_links(root, url)
    return ParsedDoc(
        title=title, text=body, doc_type="html",
        meta={"url": url, "char_count": len(body),
              "jsonld_reviews": len(jsonld_reviews),
              "file_links": files, "section_links": sections},
    )
