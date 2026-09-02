"""Таксономия тем отзывов, выведенная из корпуса, вместо списка в коде.

Было: 21 тема и 144 подстроки, написанные руками. Не масштабируется — банковских
продуктов и способов пожаловаться столько, что список придётся вечно дописывать,
и он всё равно отстаёт от того, как пишут клиенты.

Разделение труда здесь принципиальное, и оно не «отдать всё модели»:

  • КАКИЕ бывают темы — решает модель, читая сам корпус. Список порождается
    данными, а не автором кода, и пересобирается, когда корпус меняется.
  • К КАКОЙ теме относится конкретный отзыв — решает вектор. Описание темы
    эмбедится тем же bge-m3, что и отзывы, дальше обычный косинус. Размечать
    169 тыс. отзывов моделью стоило бы десятки миллионов токенов, и повторить
    это было бы нельзя; вектор считает то же самое за минуты и детерминированно.
  • СКОЛЬКО чего — считает SQL по сохранённой разметке. Панель тем это агрегат
    по всему рынку за 63 дня на каждое открытие вкладки: ни модель, ни перебор
    векторов там жить не могут.

Разметка хранится по url — тому же ключу, что в review_index, где уже есть
банк, продукт, дата и город. Поэтому агрегат собирается одним join, и в чужую
базу (куда у нас нет прав на запись) дописывать ничего не нужно.

Схема: migrations/029_review_topics.sql.
"""
from __future__ import annotations

import logging
import os
import re
import time

from sqlalchemy import text

from .. import db
from . import embedder
from .bankiru_reviews import QUERY_PREFIX, _get_engine as _source_engine

log = logging.getLogger(__name__)

_ACTIVE = "active_version"
_ASSIGNED = "assigned_at"
# Сколько тем оставить в итоговой таксономии. Не жёсткий список, а рамка: слишком
# мелкое дробление аудитору бесполезно (по теме из трёх жалоб всплеск не увидишь),
# слишком крупное — сливает разные риски в одну строку.
_TARGET = int(os.getenv("REVIEW_TOPICS_TARGET", "26"))
_BATCH_REVIEWS = int(os.getenv("REVIEW_TOPICS_BATCH", "45"))
_ROUNDS = int(os.getenv("REVIEW_TOPICS_ROUNDS", "12"))
_DRY_ROUNDS = int(os.getenv("REVIEW_TOPICS_DRY", "2"))
_ASSIGN_CHUNK = int(os.getenv("REVIEW_TOPICS_ASSIGN_CHUNK", "20000"))
# Сколько тем-кандидатов сохранять на отзыв. Больше трёх нужно не для показа, а
# для НОРМИРОВКИ: отбор кандидатов идёт по сырой оценке, а она смещена в пользу
# тем-притягивателей. Замер: у 22% отзывов нормировка меняет лучшую тему — при
# отборе топ-3 верная тема просто не доживала до момента, когда её осадят.
# Показываем всё равно три, но выбранные уже из нормированных.
_TOP_N = int(os.getenv("REVIEW_TOPICS_TOP_N", "8"))
READ_TOP = int(os.getenv("REVIEW_TOPICS_READ_TOP", "3"))
# Порог по НОРМИРОВАННОЙ оценке: на сколько своих сигм отзыв ближе к теме, чем
# средний. Применяется при ЧТЕНИИ, поэтому подбирается без повторной разметки.
MIN_Z = float(os.getenv("REVIEW_TOPICS_MIN_Z", "0.3"))
# Сколько ближайших тем считать темами отзыва. Порог и ранг управляют РАЗНЫМ, и
# это выяснилось замером: полнота («Прочее») зависит только от порога — 13 из 100
# при z>=0, 21 при z>=0.3, 31 при z>=0.6; а точность зависит только от ранга —
# 0.65 при ранге 1, 0.53 при 2, 0.46 при 3 и всего 0.16 без ограничения ранга.
# Поэтому нужны оба: порог решает, отнести ли отзыв к чему-то вообще, ранг — не
# приписывать ему восьмую по близости тему наравне с первой.
RANK_CAP = int(os.getenv("REVIEW_TOPICS_RANK_CAP", "2"))

# Второй проход пересборки — доразметка по «Прочему». Выключается, если прогон
# нужно сделать быстрым: он удваивает и чтение корпуса, и разметку.
_RESIDUAL_PASS = os.getenv("REVIEW_TOPICS_RESIDUAL", "1").lower() not in ("0", "false", "no")

# Во сколько раз позиции позволено быть размеченной чаще, чем её упоминают.
# Замер на 28 позициях: надёжные укладываются в 2.9, переборщики начинаются с
# 7.8 — между ними чистый разрыв, поэтому тройка берётся с запасом в обе стороны.
_MAX_OVER = float(os.getenv("REVIEW_PRODUCTS_MAX_OVER", "3.0"))

_RISKS = ("compliance", "conduct", "ops")


# ── Измерения таксономии ─────────────────────────────────────────────────────
# Машина одна, настроек две. Тема отвечает на вопрос «ЧТО пошло не так»,
# продукт — «С ЧЕМ это произошло». Разделять их обязательно: жалоба на задержку
# перевода это одновременно тема «задержки» и продукт «денежный перевод», и
# складывать их в один список значило бы заставить их конкурировать за ранг.
THEME, PRODUCT = "theme", "product"

_SEE_PRODUCT = (
    "Ты — методолог внутреннего аудита банка. Тебе дают выборку реальных жалоб клиентов.\n"
    "Назови БАНКОВСКИЕ ПРОДУКТЫ И УСЛУГИ, о которых идёт речь в этих жалобах: вклад, "
    "кредитная карта, ипотека, эквайринг и тому подобное.\n"
    "Требования: называй сам продукт, а НЕ проблему с ним — «ипотека», а не «задержка "
    "одобрения ипотеки», «страхование», а не «навязанная страховка»; 1-4 слова; не выдумывай "
    "продуктов, которых в выборке нет; одна строка — один продукт, без нумерации и пояснений.")

_MERGE_PRODUCT = (
    "Ты — методолог внутреннего аудита банка. Ниже черновой список банковских продуктов и "
    "услуг, собранный чтением жалоб клиентов: там есть дубли, синонимы и слишком мелкие "
    "формулировки.\n\n"
    "Сведи его в рабочий каталог продуктов. Продуктов должно быть от {lo} до {n} — это "
    "требование, а не пожелание.\n\n"
    "Правила:\n"
    "— объединяй синонимы одного продукта («карта Мир», «дебетовая карточка» — одна "
    "позиция), но НЕ сливай разные продукты: вклад и накопительный счёт это разное, "
    "кредитная карта и потребительский кредит тоже;\n"
    "— различай розницу и обслуживание бизнеса: расчётно-кассовое обслуживание и эквайринг "
    "не то же самое, что дебетовая карта физлица;\n"
    "— сохрани продукты, которые встречаются редко, но подлежат отдельной проверке "
    "(обезличенные металлические счета, индивидуальный инвестиционный счёт, страхование "
    "жизни при кредите, программа долгосрочных сбережений);\n"
    "— не превращай продукт в проблему: «навязанная страховка» — это не продукт, продукт "
    "здесь «страхование»;\n"
    "— описание описывает САМ ПРОДУКТ: что это, как он называется у разных банков, какими "
    "признаками клиент его узнаёт (на карту приходит зарплата; вклад открывают на срок под "
    "процент; по ипотеке есть залог и созаёмщики). Пиши словами клиентов, но про ПРЕДМЕТ;\n"
    "— в описании НЕ перечисляй проблемы и жалобы. Это главное правило. Проблемы у всех "
    "продуктов одинаковые — блокировки, комиссии, отказ, списание, — и описание из проблем "
    "перестаёт отличать один продукт от другого: замер показал, что позиция с описанием "
    "«жалобы на блокировки, комиссии и списания» собрала тысячи чужих обращений, потому что "
    "подходила любому. За что клиент ругает продукт, определяет ТЕМА, а не продукт;\n"
    "— описание строго до 300 символов, без кавычек и перечисления цитат;\n"
    "— каталог обязан ПОКРЫВАТЬ выборку. Если заметная часть жалоб не о конкретном "
    "продукте, а о канале обслуживания или о банке в целом — например о мобильном "
    "приложении, обслуживании в отделении, банкоматах, службе поддержки, — заведи для них "
    "отдельные позиции. Замер показал, во что обходится пропуск: без таких позиций эти "
    "обращения приписываются ближайшему по описанию продукту, и редкая позиция собирает "
    "чужое — эскроу-счёт получил 7916 обращений при 378, где слово вообще встречается.\n\n"
    "Формат: один продукт на строку, ровно три поля через вертикальную черту, без "
    "заголовков и нумерации:\n"
    "латинский_слаг | Название для аудитора | развёрнутое описание словами клиентов")


class _Dim:
    """Настройка одного измерения таксономии."""

    def __init__(self, key, see, merge, target, minimum, has_risk):
        self.key, self.see, self.merge = key, see, merge
        self.target, self.minimum, self.has_risk = target, minimum, has_risk


def _dim(name: str) -> "_Dim":
    if name == PRODUCT:
        return _Dim(PRODUCT, _SEE_PRODUCT, _MERGE_PRODUCT,
                    int(os.getenv("REVIEW_PRODUCTS_TARGET", "28")),
                    int(os.getenv("REVIEW_PRODUCTS_MIN", "12")), has_risk=False)
    return _Dim(THEME, _SEE, _MERGE, _TARGET, _MIN_TOPICS, has_risk=True)


def _skey(dim: str, k: str) -> str:
    """Ключ состояния. У тем он БЕЗ префикса — таким он и лежит на проде,
    и переименование стоило бы потери активного поколения."""
    return k if dim == THEME else f"{dim}:{k}"


def _llm(timeout: float = 180):
    from openai import OpenAI

    from ..ai.analyst import LLM_API_KEY, LLM_BASE_URL
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY не задан — таксономию не из чего выводить")
    return OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, max_retries=2, timeout=timeout)


def _ask(system: str, user: str, *, max_tokens: int = 3000) -> tuple[str, bool]:
    """Возвращает (ответ, обрезан_ли).

    Признак обрезки нужен не для лога: упёршись в лимит, модель обрывает список
    на полуслове, и в таксономию попадали мусорные обрубки вроде «Некор» и
    «Некорректное отобра». Вызывающий отбрасывает последнюю строку.
    """
    from ..ai.analyst import fast_model
    r = _llm().chat.completions.create(
        model=fast_model(),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0, max_tokens=max_tokens,
        # рассуждения выключены: задача — назвать увиденное, а thinking-токены
        # считаются в тот же бюджет и обрывают список ещё раньше
        extra_body={} if os.getenv("REVIEW_TOPICS_EFFORT", "none").lower() in ("none", "off", "")
        else {"reasoning_effort": os.getenv("REVIEW_TOPICS_EFFORT")})
    ch = r.choices[0]
    return (ch.message.content or "").strip(), (getattr(ch, "finish_reason", "") == "length")


# ── 1. Что бывает в корпусе ──────────────────────────────────────────────────
_SEE = ("Ты — методолог внутреннего аудита банка. Тебе дают выборку реальных жалоб клиентов.\n"
        "Назови ТЕМЫ, которые в них встречаются: не пересказ отдельных жалоб, а категории, по "
        "которым аудитор захочет строить статистику и ловить всплески.\n"
        "Требования: 2-5 слов на тему; тема должна быть проверяемой — по ней можно отобрать "
        "жалобы; не выдумывай тем, которых в выборке нет; одна строка — одна тема, без "
        "нумерации и пояснений.")


def _sample(n: int, offset: int = 0) -> list[str]:
    """Выборка жалоб для чтения моделью. Берём по одной на url и режем края:
    слишком короткие — обрывки, слишком длинные — многостраничные жалобы, где
    тема тонет и модель называет всё подряд."""
    eng = _source_engine()
    if eng is None:
        return []
    with eng.connect() as c:
        return c.execute(text('''
            SELECT "reviewBody" FROM (
                SELECT DISTINCT ON (url) url, "reviewBody", "datePublished"
                FROM bankiru.reviews
                WHERE length("reviewBody") BETWEEN 200 AND 2500
                ORDER BY url, "datePublished" DESC) s
            ORDER BY md5(url) OFFSET :off LIMIT :n
        '''), {"n": n, "off": offset}).scalars().all()


def _sample_residual(n: int, offset: int = 0, dim: str = THEME) -> list[str]:
    """Выборка из НЕРАЗМЕЧЕННЫХ — тех, что попадают в «Прочее».

    Смотреть на корпус целиком для доразметки бесполезно: модель снова назовёт
    то, что уже есть в таксономии, потому что это и есть большинство. Дыры видны
    только там, где вектор ни к чему не притянулся.

    Ссылки берём у себя, тексты — в источнике: зеркало текстов не хранит.
    """
    with db.session() as s:
        urls = list(s.execute(text("""
            SELECT f.url FROM review_index f
            WHERE NOT EXISTS (SELECT 1 FROM review_topic_label l
                              JOIN review_topic_def d ON d.topic_id = l.topic_id
                              WHERE l.url = f.url AND d.dim = :dim
                                AND l.z >= :z AND l.rn <= :rank)
            ORDER BY md5(f.url) OFFSET :off LIMIT :n
        """), {"dim": dim, "z": MIN_Z, "rank": RANK_CAP, "off": offset,
               "n": n}).scalars().all())
    if not urls:
        return []
    eng = _source_engine()
    if eng is None:
        return []
    with eng.connect() as c:
        return list(c.execute(text('''
            SELECT DISTINCT ON (url) "reviewBody" FROM bankiru.reviews
            WHERE url = ANY(:urls) AND length("reviewBody") BETWEEN 200 AND 2500
            ORDER BY url, "datePublished" DESC
        '''), {"urls": urls}).scalars().all())


def discover(sampler=None, dim: str = THEME) -> list[str]:
    """Раунды чтения корпуса, пока не перестанут появляться новые темы.

    Счётчик раундов не годится: редкие, но важные для аудита темы (наследование,
    исполнительные листы) в случайной выборке из полусотни жалоб просто не
    встречаются. Поэтому идём, пока подряд _DRY_ROUNDS раундов не дадут ничего
    нового — тот же приём, что и при поиске багов.
    """
    take = sampler or _sample
    spec = _dim(dim)
    seen: dict[str, str] = {}          # нормализованное → как назвала модель
    dry = 0
    for rnd in range(_ROUNDS):
        chunk = take(_BATCH_REVIEWS, rnd * _BATCH_REVIEWS)
        if not chunk:
            break
        body = "\n\n".join(f"[{i + 1}] {t[:700]}" for i, t in enumerate(chunk))
        known = "\n".join(sorted(seen.values()))
        user = body if not known else (
            f"Уже известное (называть НЕ надо, нужны только новые):\n{known}\n\n"
            f"Жалобы:\n{body}")
        try:
            raw, cut = _ask(spec.see, user)
        except Exception as e:
            log.warning("review_topics: раунд %d не удался (%s)", rnd + 1, e)
            continue
        lines = raw.splitlines()
        if cut and lines:
            lines = lines[:-1]        # оборванная на полуслове тема — не тема
        fresh = 0
        for line in lines:
            t = re.sub(r"^[\s\-—*•\d.)]+", "", line).strip().strip('"«»')
            # короче восьми символов осмысленной темы не бывает, а обрубки бывают
            if not (8 <= len(t) <= 60):
                continue
            key = re.sub(r"[^а-яёa-z]", "", t.lower())
            if key and key not in seen:
                seen[key] = t
                fresh += 1
        dry = dry + 1 if fresh == 0 else 0
        log.info("review_topics[%s]: раунд %d — новых %d, всего %d",
                 dim, rnd + 1, fresh, len(seen))
        if dry >= _DRY_ROUNDS:
            break
    return sorted(seen.values())


# ── 2. Свести в таксономию ───────────────────────────────────────────────────
_MERGE = (
    "Ты — методолог внутреннего аудита банка. Ниже черновой список тем жалоб, собранный "
    "чтением корпуса: там есть дубли, слишком мелкие и пересекающиеся формулировки.\n\n"
    "Сведи его в рабочую таксономию. Тем должно быть от {lo} до {n} — это требование, а не "
    "пожелание: таксономия из пяти пунктов аудитору бесполезна, по теме «низкое качество "
    "обслуживания» всплеск не увидишь и проверку не назначишь.\n\n"
    "Правила:\n"
    "— объединяй дубли и близкие формулировки, но НЕ сливай разные риски в одну тему: "
    "блокировка счёта по 115-ФЗ, навязанная страховка и грубость сотрудника — три разные "
    "темы, у них разные владельцы и разные проверки;\n"
    "— не объединяй темы только потому, что они про одно подразделение или один продукт;\n"
    "— сохрани темы, которые встречаются редко, но означают нарушение требований "
    "(наследование, исполнительные листы, банкротство, передача данных);\n"
    "— выкидывай темы, по которым нельзя построить осмысленную статистику;\n"
    "— риск ровно один из трёх: compliance (нарушение требований и закона, эскалация к "
    "регулятору), conduct (недобросовестная продажа и отношение к клиенту), ops "
    "(сбои процессов и обслуживания);\n"
    "— описание пиши так, как о проблеме пишут САМИ КЛИЕНТЫ, их словами и в нескольких "
    "формулировках: по этому описанию тема будет сопоставляться с текстами отзывов;\n"
    "— описание строго до 300 символов, без кавычек и перечисления цитат: длинные описания "
    "не помещаются в ответ и список обрывается на третьей теме.\n\n"
    "Формат: одна тема на строку, ровно четыре поля через вертикальную черту, без "
    "заголовков и нумерации:\n"
    "латинский_слаг | Название для аудитора | risk | развёрнутое описание темы словами клиентов")


_MIN_TOPICS = int(os.getenv("REVIEW_TOPICS_MIN", "18"))


def _parse_taxonomy(lines: list[str], dim: str = THEME) -> list[dict]:
    """Разбор ответа модели. У темы четыре поля, у продукта три: риска у
    продукта нет — «ипотека» сама по себе ничем не рискованна."""
    spec = _dim(dim)
    need = 4 if spec.has_risk else 3
    out, seen = [], set()
    for line in lines:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < need:
            continue
        key = re.sub(r"[^a-z0-9_]", "", parts[0].lower().replace("-", "_"))[:40]
        label = parts[1]
        if spec.has_risk:
            risk, descr = parts[2].lower(), " | ".join(parts[3:])
            risk = risk if risk in _RISKS else "ops"
        else:
            risk, descr = None, " | ".join(parts[2:])
        if not key or key in seen or len(label) < 3 or len(descr) < 20:
            continue
        seen.add(key)
        out.append({"key": key, "label": label, "risk": risk, "descr": descr})
    return out[:spec.target]


def finalize(names: list[str], dim: str = THEME) -> list[dict]:
    spec = _dim(dim)
    sys_prompt = spec.merge.format(n=spec.target, lo=spec.minimum)
    raw, cut = _ask(sys_prompt, "\n".join(names), max_tokens=16000)
    lines = raw.splitlines()
    if cut and lines:
        lines = lines[:-1]            # обрезанная строка даст тему без описания
    out = _parse_taxonomy(lines, dim)
    if len(out) >= spec.minimum:
        return out
    # Модель склонна читать верхнюю границу как приглашение укрупнить: первый
    # прогон дал 5 тем вместо 24. Возвращаем ей её же ответ с прямым указанием —
    # это дешевле и надёжнее, чем угадывать формулировку промпта с одного раза.
    log.info("review_topics[%s]: получено %d при минимуме %d — прошу дробнее",
             dim, len(out), spec.minimum)
    retry = (f"Ты вернул только {len(out)} позиций, а нужно минимум {spec.minimum}. "
             f"Ты слил в одну тему разные риски. Разбей укрупнённые темы обратно и верни "
             f"список заново в том же формате.\n\nТвой ответ:\n{raw}\n\nИсходный черновик:\n"
             + "\n".join(names))
    try:
        raw2, cut2 = _ask(sys_prompt, retry, max_tokens=16000)
        lines2 = raw2.splitlines()
        if cut2 and lines2:
            lines2 = lines2[:-1]
        out2 = _parse_taxonomy(lines2, dim)
        if len(out2) > len(out):
            return out2
    except Exception as e:
        log.warning("review_topics: повторное сведение не удалось (%s)", e)
    return out


# ── 3. Сохранить и посчитать векторы тем ─────────────────────────────────────
def _state_get(k: str) -> str | None:
    with db.session() as s:
        row = s.execute(text("SELECT v FROM review_topic_state WHERE k = :k"), {"k": k}).first()
    return row[0] if row else None


def _state_put(k: str, v: str) -> None:
    with db.session() as s:
        s.execute(text("""
            INSERT INTO review_topic_state (k, v, updated_at) VALUES (:k, :v, now())
            ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = now()
        """), {"k": k, "v": str(v)})


def active_version(dim: str = THEME) -> int:
    try:
        return int(_state_get(_skey(dim, _ACTIVE)) or 0)
    except (TypeError, ValueError):
        return 0


def store(taxonomy: list[dict], dim: str = THEME) -> int:
    """Пишет новое поколение таксономии и считает векторы тем.

    Эмбедим описание с QUERY-префиксом, а не как есть: векторы отзывов посчитаны
    с passage-префиксом, bge-m3 асимметричная, и без этого косинус деградирует —
    та же ловушка, что и в поиске.
    """
    if not taxonomy:
        return 0
    ver = active_version(dim) + 1
    with db.session() as s:
        for t in taxonomy:
            vec = embedder.embed_one(QUERY_PREFIX + f"{t['label']}. {t['descr']}")
            s.execute(text("""
                INSERT INTO review_topic_def (dim, version, key, label, descr, risk, embedding)
                VALUES (:dim, :v, :k, :l, :d, :r, CAST(:e AS vector))
                ON CONFLICT (dim, version, key) DO UPDATE SET
                    label = EXCLUDED.label, descr = EXCLUDED.descr,
                    risk = EXCLUDED.risk, embedding = EXCLUDED.embedding
            """), {"dim": dim, "v": ver, "k": t["key"], "l": t["label"], "d": t["descr"],
                   "r": t.get("risk"), "e": "[" + ",".join(f"{x:.6f}" for x in vec) + "]"})
    log.info("review_topics[%s]: сохранено поколение %d, позиций %d",
             dim, ver, len(taxonomy))
    return ver


# ── 4. Разложить корпус по темам ─────────────────────────────────────────────
def assign(version: int | None = None, dim: str = THEME) -> dict:
    """Каждому отзыву — ближайшие темы. Считает Postgres, не приложение.

    Идём кусками по id: один запрос на 169 тыс. отзывов × десятки тем — это
    миллионы сравнений 1024-мерных векторов в одной транзакции, он упирается в
    statement_timeout и память. Кусками ещё и перезапускаемо.

    Храним топ-N с оценками БЕЗ отсечки. Порог применяется при чтении, и его
    можно перебрать, не гоняя разметку по всему корпусу заново.
    """
    ver = version or active_version(dim)
    if not ver:
        return {"ok": False, "reason": "нет активной таксономии"}
    src = _source_engine()
    if src is None:
        return {"ok": False, "reason": "источник недоступен"}
    with db.session() as s:
        defs = s.execute(text(
            "SELECT topic_id, key, embedding FROM review_topic_def"
            " WHERE dim = :dim AND version = :v AND embedding IS NOT NULL"
            " ORDER BY topic_id"), {"dim": dim, "v": ver}).all()
    if not defs:
        return {"ok": False, "reason": "у поколения нет векторов"}
    # темы приезжают в запрос значениями: их десятки, а не миллионы
    values = ", ".join(f"({d[0]}, CAST(:t{i} AS vector))" for i, d in enumerate(defs))
    tparams = {f"t{i}": str(d[2]) for i, d in enumerate(defs)}

    t0 = time.time()
    lo, written, seen = 0, 0, 0
    with db.session() as s:
        # Чистим метки ТОЛЬКО своего измерения: безусловный DELETE снёс бы
        # разметку соседнего и обнулил бы панель тем при пересборке продуктов.
        s.execute(text("""
            DELETE FROM review_topic_label WHERE topic_id IN
                (SELECT topic_id FROM review_topic_def WHERE dim = :dim)"""),
            {"dim": dim})
    while True:
        with src.connect() as c:
            c.execute(text("SET LOCAL statement_timeout = 600000"))
            rows = c.execute(text(f"""
                WITH t(topic_id, vec) AS (VALUES {values}),
                     rev AS (
                        SELECT DISTINCT ON (r.url) r.url, r.id AS rid, e.embedding AS emb
                        FROM bankiru.reviews r
                        JOIN bankiru.review_embeddings e ON e.review_id = r.id
                        WHERE r.id > :lo AND r.id <= :hi
                          AND length(r."reviewBody") >= 40
                        ORDER BY r.url, r."datePublished" DESC)
                SELECT url, topic_id, d FROM (
                    SELECT rev.url, t.topic_id, (rev.emb <=> t.vec) AS d,
                           row_number() OVER (PARTITION BY rev.url
                                              ORDER BY rev.emb <=> t.vec) AS rn
                    FROM rev CROSS JOIN t) z
                WHERE rn <= :topn
            """), {**tparams, "lo": lo, "hi": lo + _ASSIGN_CHUNK, "topn": _TOP_N}).all()
        if rows:
            payload = [{"u": r[0], "t": int(r[1]), "s": max(0.0, 1.0 - float(r[2]))} for r in rows]
            with db.session() as s:
                s.execute(text("""
                    INSERT INTO review_topic_label (url, topic_id, score)
                    VALUES (:u, :t, :s)
                    ON CONFLICT (url, topic_id) DO UPDATE SET score = EXCLUDED.score
                """), payload)
            written += len(payload)
            seen += len({r[0] for r in rows})
        lo += _ASSIGN_CHUNK
        if lo > _max_source_id():
            break
    _normalize(dim)
    _state_put(_skey(dim, _ASSIGNED), str(int(time.time())))
    dt = time.time() - t0
    log.info("review_topics[%s]: размечено отзывов %d, меток %d, %.0f с",
             dim, seen, written, dt)
    return {"ok": True, "reviews": seen, "labels": written,
            "version": ver, "seconds": round(dt, 1)}


def label_new(batch: int = 4000, dim: str = THEME) -> dict:
    """Инкрементальная разметка: только отзывы БЕЗ меток, свежие первыми.

    Полный assign() перегоняет весь корпус (DELETE + все куски) и запускается
    вручную при смене поколения; между поколениями новые отзывы оставались без
    меток — 05.08.2026 это было 36 процентов недельного окна, и сигналы главной
    на такой разметке врут. Вдобавок assign() ходит ТОЛЬКО во внешний корпус:
    отзывы наших коллекторов (finuslugi/sravni/bankiros, вектора в
    review_embedding) он не видит вовсе — здесь размечаются обе стороны.

    z считаем по СНИМКУ текущей статистики темы (avg/std сохранённых меток):
    корпус большой, статистика дрейфует медленно; полный пересчёт остаётся за
    rebuild()/assign(). rn — ранг по z внутри меток отзыва, как в _normalize."""
    ver = active_version(dim)
    if not ver:
        return {"ok": False, "reason": "нет активной таксономии"}
    with db.session() as s:
        defs = s.execute(text(
            "SELECT topic_id, embedding FROM review_topic_def"
            " WHERE dim = :dim AND version = :v AND embedding IS NOT NULL"
            " ORDER BY topic_id"), {"dim": dim, "v": ver}).all()
        stats = {int(r[0]): (float(r[1]), float(r[2])) for r in s.execute(text("""
            SELECT l.topic_id, avg(l.score),
                   coalesce(nullif(stddev_samp(l.score), 0), 1)
            FROM review_topic_label l
            JOIN review_topic_def d ON d.topic_id = l.topic_id
            WHERE d.dim = :dim GROUP BY l.topic_id"""), {"dim": dim})}
        # «Ещё не размечен» — в СВОЁМ измерении: отзыв с темами, но без продукта
        # обязан попасть в доразметку продукта.
        todo = s.execute(text("""
            SELECT i.url, i.review_id, i.source FROM review_index i
            WHERE NOT EXISTS (SELECT 1 FROM review_topic_label l
                              JOIN review_topic_def d ON d.topic_id = l.topic_id
                              WHERE l.url = i.url AND d.dim = :dim)
              AND i.dt IS NOT NULL AND i.dt <= now()
            ORDER BY i.dt DESC LIMIT :lim"""), {"dim": dim, "lim": batch}).all()
    if not defs or not todo:
        return {"ok": True, "labeled": 0, "backlog": 0}
    values = ", ".join(f"({d[0]}, CAST(:t{i} AS vector))" for i, d in enumerate(defs))
    tparams = {f"t{i}": str(d[1]) for i, d in enumerate(defs)}

    # score-кандидаты по каждой стороне: считает Postgres той БД, где вектора
    scored: dict[str, list[tuple[int, float]]] = {}   # url -> [(topic_id, score)]

    loc_urls = [u for u, _rid, src_ in todo if src_ != "bankiru"]
    if loc_urls:
        with db.session() as s:
            for r in s.execute(text(f"""
                WITH t(topic_id, vec) AS (VALUES {values})
                SELECT re.url, t.topic_id, (re.embedding <=> t.vec) AS d
                FROM review_embedding re CROSS JOIN t
                WHERE re.url = ANY(:us)
            """), {**tparams, "us": loc_urls}):
                scored.setdefault(r[0], []).append(
                    (int(r[1]), max(0.0, 1.0 - float(r[2]))))

    ext = [(u, int(rid)) for u, rid, src_ in todo if src_ == "bankiru"]
    if ext:
        src_eng = _source_engine()
        if src_eng is not None:
            url_by_id = {rid: u for u, rid in ext}
            with src_eng.connect() as c:
                for r in c.execute(text(f"""
                    WITH t(topic_id, vec) AS (VALUES {values})
                    SELECT e.review_id, t.topic_id, (e.embedding <=> t.vec) AS d
                    FROM bankiru.review_embeddings e CROSS JOIN t
                    WHERE e.review_id = ANY(:ids)
                """), {**tparams, "ids": list(url_by_id)}):
                    u = url_by_id.get(int(r[0]))
                    if u:
                        scored.setdefault(u, []).append(
                            (int(r[1]), max(0.0, 1.0 - float(r[2]))))

    payload = []
    for url, cand in scored.items():
        cand.sort(key=lambda x: -x[1])
        top = cand[:_TOP_N]
        zs = []
        for tid, sc in top:
            m, sd = stats.get(tid, (0.55, 1.0))
            zs.append((tid, sc, (sc - m) / sd))
        zs.sort(key=lambda x: -x[2])
        for rn, (tid, sc, z) in enumerate(zs, start=1):
            payload.append({"u": url, "t": tid, "s": round(sc, 6),
                            "z": round(z, 4), "r": rn})
    if payload:
        with db.session() as s:
            s.execute(text("""
                INSERT INTO review_topic_label (url, topic_id, score, z, rn)
                VALUES (:u, :t, :s, :z, :r)
                ON CONFLICT (url, topic_id) DO UPDATE SET
                    score = EXCLUDED.score, z = EXCLUDED.z, rn = EXCLUDED.rn
            """), payload)
    # отзывы без вектора (эмбеддинг ещё не посчитан) остаются в бэклоге —
    # embed_missing() их догонит, разметим следующим тиком
    backlog = len(todo) - len(scored)
    if scored:
        log.info("review_topics: инкрементально размечено %d (меток %d), без вектора %d",
                 len(scored), len(payload), backlog)
    return {"ok": True, "labeled": len(scored), "labels": len(payload),
            "backlog": backlog, "version": ver}


def _normalize(dim: str = THEME) -> None:
    """Приводит оценки тем к сопоставимому виду.

    Сырой косинус между темами сравнивать нельзя: у каждой темы своя
    «притягательность». Замер на проде — средние по темам от 0.543 до 0.583 при
    разбросе ±0.035, и тема «Задержки и неисполнение операций» собирала 103 тыс.
    отзывов из 169 тыс. просто потому, что её формулировка близка к любой жалобе.

    Считаем, на сколько СВОИХ стандартных отклонений отзыв ближе к теме, чем
    средний отзыв корпуса. После этого «редкая тема, но точное попадание» встаёт
    выше «темы-притягивателя со средним сходством», и порог наконец что-то
    значит одинаково для всех тем.
    """
    with db.session() as s:
        s.execute(text("""
            WITH mine AS (
                SELECT l.url, l.topic_id, l.score FROM review_topic_label l
                JOIN review_topic_def d ON d.topic_id = l.topic_id
                WHERE d.dim = :dim),
            st AS (
                SELECT topic_id, avg(score) m, nullif(stddev_samp(score), 0) sd
                FROM mine GROUP BY topic_id)
            UPDATE review_topic_label l
               SET z = (l.score - st.m) / coalesce(st.sd, 1.0)
              FROM st WHERE st.topic_id = l.topic_id
        """), {"dim": dim})
        # Ранг по нормированной оценке. Кандидатов на отзыв храним восемь, но
        # тема отзыва — это первые из них, а не все, кто перевалил порог.
        # Замер точности (доля отзывов темы, где есть её бесспорное слово):
        # ранг 1 — 65%, ранг 2 — 53%, ранг 3 — 46%, без ограничения ранга — 16%.
        # Порог один такого отсева не даёт: он режет хвост по величине, а не по
        # месту, и восьмая тема отзыва проходит наравне с первой.
        # Ранг считаем ВНУТРИ измерения. Иначе продукт отзыва конкурировал бы
        # с его темами за одни и те же места, и при RANK_CAP=2 у отзыва с двумя
        # уверенными темами продукт не поместился бы вовсе.
        s.execute(text("""
            WITH r AS (
                SELECT l.url, l.topic_id,
                       row_number() OVER (PARTITION BY l.url ORDER BY l.z DESC) rn
                FROM review_topic_label l
                JOIN review_topic_def d ON d.topic_id = l.topic_id
                WHERE d.dim = :dim)
            UPDATE review_topic_label l SET rn = r.rn
              FROM r WHERE r.url = l.url AND r.topic_id = l.topic_id
        """), {"dim": dim})


_TERMS = (
    "Ты — методолог внутреннего аудита банка. Ниже позиции каталога банковских "
    "продуктов и услуг.\n\n"
    "Для каждой позиции назови слова, которыми клиент называет её В ТЕКСТЕ ЖАЛОБЫ. "
    "Это нужно, чтобы отличить позицию, которую клиент НАЗЫВАЕТ прямо («эскроу», "
    "«ячейка», «автокредит»), от той, которую он только ОПИСЫВАЕТ, не называя "
    "(про службу поддержки пишут «не дозвонился», «оператор нахамил»).\n\n"
    "Правила:\n"
    "— 2-6 слов на позицию, в начальной форме, через запятую;\n"
    "— только слова, ОДНОЗНАЧНО указывающие на эту позицию. «Счёт» и «банк» не "
    "годятся: они встречаются у всех;\n"
    "— если позицию клиенты обычно НЕ называют, а только описывают, поставь "
    "прочерк вместо слов. Это не недостаток позиции, это её свойство.\n\n"
    "Формат: одна позиция на строку, два поля через вертикальную черту:\n"
    "латинский_слаг | слово, слово, слово")


def name_terms(dim: str = PRODUCT) -> dict:
    """Спрашивает у модели опорные слова позиций и сохраняет их.

    Отдельный дешёвый вызов, а не поле в сведении: слова нужны уже к готовому
    каталогу, и переспрашивать их не значит пересобирать таксономию.
    """
    ver = active_version(dim)
    items = topics(dim=dim)
    if not items:
        return {"ok": False, "reason": "нет таксономии"}
    listing = "\n".join(f"{t['key']} | {t['label']} — {t['descr'][:120]}" for t in items)
    raw, _ = _ask(_TERMS, listing, max_tokens=4000)
    got = 0
    with db.session() as s:
        for line in raw.splitlines():
            parts = [x.strip() for x in line.split("|")]
            if len(parts) < 2:
                continue
            key = re.sub(r"[^a-z0-9_]", "", parts[0].lower())
            words = [w.strip().lower() for w in parts[1].split(",")
                     if len(w.strip()) >= 3 and w.strip() != "-"]
            if not key:
                continue
            s.execute(text("""
                UPDATE review_topic_def SET terms = :t
                 WHERE dim = :dim AND version = :v AND key = :k"""),
                {"t": words or None, "dim": dim, "v": ver, "k": key})
            got += 1
    log.info("review_topics[%s]: опорные слова у %d позиций", dim, got)
    return {"ok": True, "positions": got, "version": ver}


def product_reliability() -> list[dict]:
    """Для каждой позиции — сколько обращений её УПОМИНАЮТ и сколько размечено.

    Позицию, которую клиенты называют прямо, но которой приписано кратно больше
    обращений, чем её вообще упоминают, показывать нельзя: она собирает чужое.
    Позиции без опорных слов не проверяются — их и не называют.
    """
    ver = active_version(PRODUCT)
    out = []
    with db.session() as s:
        rows = s.execute(text(
            "SELECT key, label, terms FROM review_topic_def"
            " WHERE dim = :dim AND version = :v"), {"dim": PRODUCT, "v": ver}).all()
        for key, label, terms in rows:
            n = int(s.execute(text("""
                SELECT count(DISTINCT l.url) FROM review_topic_label l
                JOIN review_topic_def d ON d.topic_id = l.topic_id
                WHERE d.dim = :dim AND d.version = :v AND d.key = :k
                  AND l.rn = 1 AND l.z >= :z"""),
                {"dim": PRODUCT, "v": ver, "k": key, "z": MIN_Z}).scalar() or 0)
            support = None
            if terms:
                # Опорные слова бывают словосочетаниями («дебетовая карта»), а
                # to_tsquery такого не принимает. websearch_to_tsquery понимает
                # и кавычки-фразу, и OR — тот же разбор, что у поиска по базе
                # знаний, поэтому поведение совпадает с тем, что видит аудитор.
                q = " OR ".join('"%s"' % w.replace('"', " ") for w in terms)
                support = int(s.execute(text(
                    "SELECT count(*) FROM review_index"
                    " WHERE tsv @@ websearch_to_tsquery("
                    "     CAST('russian' AS regconfig), :q)"),
                    {"q": q}).scalar() or 0)
            out.append({"key": key, "label": label, "terms": terms,
                        "labeled": n, "support": support,
                        "ratio": (n / support) if support else None})
    return out


def apply_product_labels() -> dict:
    """Переносит нашу разметку продукта в review_index.product.

    Колонка product в индексе была копией метки источника, а та неверна у
    подавляющего большинства обращений. Кладём в неё ЛУЧШИЙ по нормированной
    оценке продукт из нашей разметки — и весь код, который уже фильтрует и
    группирует по этой колонке, начинает работать по верным данным без правок.

    Берём строго rn = 1: у обращения один продукт, в отличие от темы. Жалоба
    бывает и про задержку, и про комиссию сразу, но «вклад и ипотека
    одновременно» — это уже не факт о продукте, а неуверенность разметки.
    Не дотянувшие до порога остаются без продукта: пустое честнее ложного.
    """
    ver = active_version(PRODUCT)
    if not ver:
        return {"ok": False, "reason": "нет разметки продукта"}
    # Позиции, которым приписано кратно больше обращений, чем их вообще
    # упоминают, в индекс не пишем: они собирают чужое. Правило считается
    # ЗАМЕРОМ на каждом переносе, а не списком в коде, поэтому переживает
    # пересборку таксономии. Позиции без опорных слов (их не называют, а
    # описывают — служба поддержки) проверке не подлежат.
    unreliable = [r["key"] for r in product_reliability()
                  if r["ratio"] is not None and r["ratio"] > _MAX_OVER]
    if unreliable:
        log.info("review_topics: не переносим %d позиций-переборщиков: %s",
                 len(unreliable), ", ".join(unreliable))
    with db.session() as s:
        n = s.execute(text("""
            WITH best AS (
                SELECT l.url, d.label
                  FROM review_topic_label l
                  JOIN review_topic_def d ON d.topic_id = l.topic_id
                 WHERE d.dim = :dim AND d.version = :v
                   AND l.rn = 1 AND l.z >= :minz
                   AND NOT (d.key = ANY(:skip)))
            UPDATE review_index i
               SET product = best.label
              FROM best WHERE best.url = i.url
                AND (i.product IS DISTINCT FROM best.label)
        """), {"dim": PRODUCT, "v": ver, "minz": MIN_Z,
               "skip": unreliable}).rowcount
        # Обращения, которые ни к чему не отнеслись, не должны сохранять старую
        # ложную метку: иначе в срезе «Обслуживание юридических лиц» осталась бы
        # именно та розница, ради которой всё и затевалось.
        cleared = s.execute(text("""
            UPDATE review_index i SET product = NULL
             WHERE i.product IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM review_topic_label l
                               JOIN review_topic_def d ON d.topic_id = l.topic_id
                               WHERE l.url = i.url AND d.dim = :dim
                                 AND d.version = :v AND l.rn = 1 AND l.z >= :minz
                                 AND NOT (d.key = ANY(:skip)))
        """), {"dim": PRODUCT, "v": ver, "minz": MIN_Z,
               "skip": unreliable}).rowcount
    log.info("review_topics: продукт проставлен %d обращениям, снят у %d", n, cleared)
    return {"ok": True, "labeled": n, "cleared": cleared, "version": ver,
            "skipped": unreliable}


def _max_source_id() -> int:
    src = _source_engine()
    if src is None:
        return 0
    with src.connect() as c:
        return int(c.execute(text("SELECT coalesce(max(id), 0) FROM bankiru.reviews")).scalar() or 0)


def seed_names(dim: str = THEME) -> list[str]:
    """С чего начинать сведение, помимо прочитанного в корпусе.

    Случайная выборка не встречает редкое: наследование, исполнительные листы,
    банкротство, программа долгосрочных сбережений попадаются в жалобах единицы
    раз на тысячу, но именно они — предмет проверки. Первый прогон таксономии их
    и потерял.

    Поэтому в сведение уезжает то, что уже известно: прошлое поколение
    таксономии, а на самом первом прогоне — список тем, написанный руками. Он не
    выбрасывается и не остаётся истиной: он перестаёт быть РЕЗУЛЬТАТОМ и
    становится ВХОДОМ. Накопленный аудиторский опыт сохраняется, а дописывать
    список руками больше не нужно — дальше поколения наследуют друг друга.
    """
    prev = topics(dim=dim)
    if prev:
        return [t["label"] for t in prev]
    if dim == PRODUCT:
        # Метки продукта в корпусе расставлены неверно, но САМ СЛОВАРЬ там
        # правильный: «Ипотека», «Эквайринг», «Кредитная карта» — настоящие
        # названия продуктов. Берём словарь как вход, а раскладывать по нему
        # будет вектор, а не чужая метка.
        with db.session() as s:
            return [r for r in s.execute(text(
                "SELECT DISTINCT product FROM review_index"
                " WHERE product IS NOT NULL AND length(product) > 2")).scalars().all()]
    from .reviews_dash import THEMES         # ленивый импорт: иначе кольцо
    return [t["label"] for t in THEMES]


def rebuild(dim: str = THEME) -> dict:
    """Полный цикл: прочитать корпус → свести таксономию → сохранить → разметить."""
    spec = _dim(dim)
    names = discover(dim=dim)
    if not names:
        return {"ok": False, "reason": "модель не назвала ни одной позиции"}
    names = sorted(set(names) | set(seed_names(dim)))
    taxonomy = finalize(names, dim)
    if not taxonomy:
        return {"ok": False, "reason": "таксономия не собралась"}
    # Предохранитель. Один неудачный прогон модели не должен ухудшать прод: был
    # случай, когда сведение схлопнулось до 3 тем (модель писала описания
    # цитатами, ответ упёрся в лимит токенов на третьей строке) — и такая
    # таксономия стала активной, а вся разметка обнулилась в три метки на всё.
    # Вырожденное поколение сохраняем для разбора, но НЕ активируем.
    prev = topics(dim=dim)
    if len(taxonomy) < spec.minimum and prev:
        store(taxonomy, dim)
        log.warning("review_topics[%s]: сведение дало %d при минимуме %d — оставляю "
                    "поколение %d, новое сохранено без активации",
                    dim, len(taxonomy), spec.minimum, active_version(dim))
        return {"ok": False, "reason": "вырожденная таксономия, прежняя оставлена",
                "got": len(taxonomy), "need": spec.minimum}
    ver = store(taxonomy, dim)
    _state_put(_skey(dim, _ACTIVE), str(ver))
    res = assign(ver, dim)

    # Второй проход — по «Прочему». Смотреть на весь корпус второй раз
    # бесполезно: модель снова назовёт то, что уже в таксономии, потому что это
    # и есть большинство. Дыры видны только там, где вектор ни к чему не
    # притянулся, а туда попадает и по-настоящему пропущенное (занижение оценки
    # залога, сроки аккредитива), и просто более дробные грани известных тем.
    # Отличить одно от другого предоставляем тому же сведению: дробное оно
    # схлопнет обратно, а новое оставит.
    if _RESIDUAL_PASS:
        extra = discover(
            sampler=lambda n, off=0: _sample_residual(n, off, dim), dim=dim)
        if extra:
            merged = finalize(
                sorted(set(names) | set(extra) | set(t["label"] for t in taxonomy)), dim)
            if len(merged) >= max(spec.minimum, len(taxonomy)):
                ver = store(merged, dim)
                _state_put(_skey(dim, _ACTIVE), str(ver))
                res = assign(ver, dim)
                taxonomy = merged
                log.info("review_topics[%s]: второй проход дал %d кандидатов из "
                         "«Прочего», таксономия выросла до %d", dim, len(extra), len(merged))
            else:
                log.info("review_topics[%s]: второй проход не улучшил таксономию (%d) — "
                         "оставляю поколение %d", dim, len(merged), ver)
    res["candidates"] = len(names)
    res["topics"] = len(taxonomy)
    return res


# ── Чтение ───────────────────────────────────────────────────────────────────
def topics(version: int | None = None, dim: str = THEME) -> list[dict]:
    ver = version or active_version(dim)
    if not ver:
        return []
    with db.session() as s:
        rows = s.execute(text(
            "SELECT topic_id, key, label, risk, descr FROM review_topic_def"
            " WHERE dim = :dim AND version = :v ORDER BY topic_id"),
            {"dim": dim, "v": ver}).mappings().all()
    return [dict(r) for r in rows]


def is_ready(dim: str = THEME) -> bool:
    """Есть ли разметка. Пока нет — вкладка обязана работать по-старому."""
    if not active_version(dim):
        return False
    try:
        with db.session() as s:
            return bool(s.execute(text("""
                SELECT 1 FROM review_topic_label l
                JOIN review_topic_def d ON d.topic_id = l.topic_id
                WHERE d.dim = :dim LIMIT 1"""), {"dim": dim}).first())
    except Exception:
        return False


def status(dim: str = THEME) -> dict:
    out = {"dim": dim, "version": active_version(dim), "topics": 0, "labels": 0,
           "reviews": 0, "assigned_at": _state_get(_skey(dim, _ASSIGNED))}
    try:
        with db.session() as s:
            out["topics"] = int(s.execute(text(
                "SELECT count(*) FROM review_topic_def"
                " WHERE dim = :dim AND version = :v"),
                {"dim": dim, "v": out["version"]}).scalar() or 0)
            out["labels"] = int(s.execute(text("""
                SELECT count(*) FROM review_topic_label l
                JOIN review_topic_def d ON d.topic_id = l.topic_id
                WHERE d.dim = :dim"""), {"dim": dim}).scalar() or 0)
            out["reviews"] = int(s.execute(text("""
                SELECT count(DISTINCT l.url) FROM review_topic_label l
                JOIN review_topic_def d ON d.topic_id = l.topic_id
                WHERE d.dim = :dim AND l.z >= :m AND l.rn <= :r"""),
                {"dim": dim, "m": MIN_Z, "r": RANK_CAP}).scalar() or 0)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out
