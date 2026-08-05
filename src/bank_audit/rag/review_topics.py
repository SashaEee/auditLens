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

_RISKS = ("compliance", "conduct", "ops")


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


def _sample_residual(n: int, offset: int = 0) -> list[str]:
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
                              WHERE l.url = f.url AND l.z >= :z AND l.rn <= :rank)
            ORDER BY md5(f.url) OFFSET :off LIMIT :n
        """), {"z": MIN_Z, "rank": RANK_CAP, "off": offset,
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


def discover(sampler=None) -> list[str]:
    """Раунды чтения корпуса, пока не перестанут появляться новые темы.

    Счётчик раундов не годится: редкие, но важные для аудита темы (наследование,
    исполнительные листы) в случайной выборке из полусотни жалоб просто не
    встречаются. Поэтому идём, пока подряд _DRY_ROUNDS раундов не дадут ничего
    нового — тот же приём, что и при поиске багов.
    """
    take = sampler or _sample
    seen: dict[str, str] = {}          # нормализованное → как назвала модель
    dry = 0
    for rnd in range(_ROUNDS):
        chunk = take(_BATCH_REVIEWS, rnd * _BATCH_REVIEWS)
        if not chunk:
            break
        body = "\n\n".join(f"[{i + 1}] {t[:700]}" for i, t in enumerate(chunk))
        known = "\n".join(sorted(seen.values()))
        user = body if not known else (
            f"Уже известные темы (их называть НЕ надо, нужны только новые):\n{known}\n\n"
            f"Жалобы:\n{body}")
        try:
            raw, cut = _ask(_SEE, user)
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
        log.info("review_topics: раунд %d — новых тем %d, всего %d", rnd + 1, fresh, len(seen))
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


def _parse_taxonomy(lines: list[str]) -> list[dict]:
    out, seen = [], set()
    for line in lines:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        key = re.sub(r"[^a-z0-9_]", "", parts[0].lower().replace("-", "_"))[:40]
        label, risk, descr = parts[1], parts[2].lower(), " | ".join(parts[3:])
        if not key or key in seen or len(label) < 3 or len(descr) < 20:
            continue
        seen.add(key)
        out.append({"key": key, "label": label,
                    "risk": risk if risk in _RISKS else "ops", "descr": descr})
    return out[:_TARGET]


def finalize(names: list[str]) -> list[dict]:
    sys_prompt = _MERGE.format(n=_TARGET, lo=_MIN_TOPICS)
    raw, cut = _ask(sys_prompt, "\n".join(names), max_tokens=16000)
    lines = raw.splitlines()
    if cut and lines:
        lines = lines[:-1]            # обрезанная строка даст тему без описания
    out = _parse_taxonomy(lines)
    if len(out) >= _MIN_TOPICS:
        return out
    # Модель склонна читать верхнюю границу как приглашение укрупнить: первый
    # прогон дал 5 тем вместо 24. Возвращаем ей её же ответ с прямым указанием —
    # это дешевле и надёжнее, чем угадывать формулировку промпта с одного раза.
    log.info("review_topics: получено %d тем при минимуме %d — прошу дробнее",
             len(out), _MIN_TOPICS)
    retry = (f"Ты вернул только {len(out)} тем, а нужно минимум {_MIN_TOPICS}. "
             f"Ты слил в одну тему разные риски. Разбей укрупнённые темы обратно и верни "
             f"список заново в том же формате.\n\nТвой ответ:\n{raw}\n\nИсходный черновик:\n"
             + "\n".join(names))
    try:
        raw2, cut2 = _ask(sys_prompt, retry, max_tokens=16000)
        lines2 = raw2.splitlines()
        if cut2 and lines2:
            lines2 = lines2[:-1]
        out2 = _parse_taxonomy(lines2)
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


def active_version() -> int:
    try:
        return int(_state_get(_ACTIVE) or 0)
    except (TypeError, ValueError):
        return 0


def store(taxonomy: list[dict]) -> int:
    """Пишет новое поколение таксономии и считает векторы тем.

    Эмбедим описание с QUERY-префиксом, а не как есть: векторы отзывов посчитаны
    с passage-префиксом, bge-m3 асимметричная, и без этого косинус деградирует —
    та же ловушка, что и в поиске.
    """
    if not taxonomy:
        return 0
    ver = active_version() + 1
    with db.session() as s:
        for t in taxonomy:
            vec = embedder.embed_one(QUERY_PREFIX + f"{t['label']}. {t['descr']}")
            s.execute(text("""
                INSERT INTO review_topic_def (version, key, label, descr, risk, embedding)
                VALUES (:v, :k, :l, :d, :r, CAST(:e AS vector))
                ON CONFLICT (version, key) DO UPDATE SET
                    label = EXCLUDED.label, descr = EXCLUDED.descr,
                    risk = EXCLUDED.risk, embedding = EXCLUDED.embedding
            """), {"v": ver, "k": t["key"], "l": t["label"], "d": t["descr"],
                   "r": t["risk"], "e": "[" + ",".join(f"{x:.6f}" for x in vec) + "]"})
    log.info("review_topics: сохранено поколение %d, тем %d", ver, len(taxonomy))
    return ver


# ── 4. Разложить корпус по темам ─────────────────────────────────────────────
def assign(version: int | None = None) -> dict:
    """Каждому отзыву — ближайшие темы. Считает Postgres, не приложение.

    Идём кусками по id: один запрос на 169 тыс. отзывов × десятки тем — это
    миллионы сравнений 1024-мерных векторов в одной транзакции, он упирается в
    statement_timeout и память. Кусками ещё и перезапускаемо.

    Храним топ-N с оценками БЕЗ отсечки. Порог применяется при чтении, и его
    можно перебрать, не гоняя разметку по всему корпусу заново.
    """
    ver = version or active_version()
    if not ver:
        return {"ok": False, "reason": "нет активной таксономии"}
    src = _source_engine()
    if src is None:
        return {"ok": False, "reason": "источник недоступен"}
    with db.session() as s:
        defs = s.execute(text(
            "SELECT topic_id, key, embedding FROM review_topic_def"
            " WHERE version = :v AND embedding IS NOT NULL ORDER BY topic_id"),
            {"v": ver}).all()
    if not defs:
        return {"ok": False, "reason": "у поколения нет векторов"}
    # темы приезжают в запрос значениями: их десятки, а не миллионы
    values = ", ".join(f"({d[0]}, CAST(:t{i} AS vector))" for i, d in enumerate(defs))
    tparams = {f"t{i}": str(d[2]) for i, d in enumerate(defs)}

    t0 = time.time()
    lo, written, seen = 0, 0, 0
    with db.session() as s:
        s.execute(text("DELETE FROM review_topic_label"))
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
    _normalize()
    _state_put(_ASSIGNED, str(int(time.time())))
    dt = time.time() - t0
    log.info("review_topics: размечено отзывов %d, меток %d, %.0f с", seen, written, dt)
    return {"ok": True, "reviews": seen, "labels": written,
            "version": ver, "seconds": round(dt, 1)}


def label_new(batch: int = 4000) -> dict:
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
    ver = active_version()
    if not ver:
        return {"ok": False, "reason": "нет активной таксономии"}
    with db.session() as s:
        defs = s.execute(text(
            "SELECT topic_id, embedding FROM review_topic_def"
            " WHERE version = :v AND embedding IS NOT NULL ORDER BY topic_id"),
            {"v": ver}).all()
        stats = {int(r[0]): (float(r[1]), float(r[2])) for r in s.execute(text("""
            SELECT topic_id, avg(score), coalesce(nullif(stddev_samp(score), 0), 1)
            FROM review_topic_label GROUP BY topic_id"""))}
        todo = s.execute(text("""
            SELECT i.url, i.review_id, i.source FROM review_index i
            WHERE NOT EXISTS (SELECT 1 FROM review_topic_label l WHERE l.url = i.url)
              AND i.dt IS NOT NULL AND i.dt <= now()
            ORDER BY i.dt DESC LIMIT :lim"""), {"lim": batch}).all()
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


def _normalize() -> None:
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
            WITH st AS (
                SELECT topic_id, avg(score) m, nullif(stddev_samp(score), 0) sd
                FROM review_topic_label GROUP BY topic_id)
            UPDATE review_topic_label l
               SET z = (l.score - st.m) / coalesce(st.sd, 1.0)
              FROM st WHERE st.topic_id = l.topic_id
        """))
        # Ранг по нормированной оценке. Кандидатов на отзыв храним восемь, но
        # тема отзыва — это первые из них, а не все, кто перевалил порог.
        # Замер точности (доля отзывов темы, где есть её бесспорное слово):
        # ранг 1 — 65%, ранг 2 — 53%, ранг 3 — 46%, без ограничения ранга — 16%.
        # Порог один такого отсева не даёт: он режет хвост по величине, а не по
        # месту, и восьмая тема отзыва проходит наравне с первой.
        s.execute(text("""
            WITH r AS (
                SELECT url, topic_id,
                       row_number() OVER (PARTITION BY url ORDER BY z DESC) rn
                FROM review_topic_label)
            UPDATE review_topic_label l SET rn = r.rn
              FROM r WHERE r.url = l.url AND r.topic_id = l.topic_id
        """))


def _max_source_id() -> int:
    src = _source_engine()
    if src is None:
        return 0
    with src.connect() as c:
        return int(c.execute(text("SELECT coalesce(max(id), 0) FROM bankiru.reviews")).scalar() or 0)


def seed_names() -> list[str]:
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
    prev = topics()
    if prev:
        return [t["label"] for t in prev]
    from .reviews_dash import THEMES         # ленивый импорт: иначе кольцо
    return [t["label"] for t in THEMES]


def rebuild() -> dict:
    """Полный цикл: прочитать корпус → свести таксономию → сохранить → разметить."""
    names = discover()
    if not names:
        return {"ok": False, "reason": "модель не назвала ни одной темы"}
    names = sorted(set(names) | set(seed_names()))
    taxonomy = finalize(names)
    if not taxonomy:
        return {"ok": False, "reason": "таксономия не собралась"}
    # Предохранитель. Один неудачный прогон модели не должен ухудшать прод: был
    # случай, когда сведение схлопнулось до 3 тем (модель писала описания
    # цитатами, ответ упёрся в лимит токенов на третьей строке) — и такая
    # таксономия стала активной, а вся разметка обнулилась в три метки на всё.
    # Вырожденное поколение сохраняем для разбора, но НЕ активируем.
    prev = topics()
    if len(taxonomy) < _MIN_TOPICS and prev:
        store(taxonomy)
        log.warning("review_topics: сведение дало %d тем при минимуме %d — оставляю "
                    "поколение %d, новое сохранено без активации",
                    len(taxonomy), _MIN_TOPICS, active_version())
        return {"ok": False, "reason": "вырожденная таксономия, прежняя оставлена",
                "got": len(taxonomy), "need": _MIN_TOPICS}
    ver = store(taxonomy)
    _state_put(_ACTIVE, str(ver))
    res = assign(ver)

    # Второй проход — по «Прочему». Смотреть на весь корпус второй раз
    # бесполезно: модель снова назовёт то, что уже в таксономии, потому что это
    # и есть большинство. Дыры видны только там, где вектор ни к чему не
    # притянулся, а туда попадает и по-настоящему пропущенное (занижение оценки
    # залога, сроки аккредитива), и просто более дробные грани известных тем.
    # Отличить одно от другого предоставляем тому же сведению: дробное оно
    # схлопнет обратно, а новое оставит.
    if _RESIDUAL_PASS:
        extra = discover(sampler=_sample_residual)
        if extra:
            merged = finalize(sorted(set(names) | set(extra) | set(t["label"] for t in taxonomy)))
            if len(merged) >= max(_MIN_TOPICS, len(taxonomy)):
                ver = store(merged)
                _state_put(_ACTIVE, str(ver))
                res = assign(ver)
                taxonomy = merged
                log.info("review_topics: второй проход дал %d кандидатов из «Прочего», "
                         "таксономия выросла до %d тем", len(extra), len(merged))
            else:
                log.info("review_topics: второй проход не улучшил таксономию (%d тем) — "
                         "оставляю поколение %d", len(merged), ver)
    res["candidates"] = len(names)
    res["topics"] = len(taxonomy)
    return res


# ── Чтение ───────────────────────────────────────────────────────────────────
def topics(version: int | None = None) -> list[dict]:
    ver = version or active_version()
    if not ver:
        return []
    with db.session() as s:
        rows = s.execute(text(
            "SELECT topic_id, key, label, risk, descr FROM review_topic_def"
            " WHERE version = :v ORDER BY topic_id"), {"v": ver}).mappings().all()
    return [dict(r) for r in rows]


def is_ready() -> bool:
    """Есть ли разметка. Пока нет — вкладка обязана работать по-старому."""
    if not active_version():
        return False
    try:
        with db.session() as s:
            return bool(s.execute(text("SELECT 1 FROM review_topic_label LIMIT 1")).first())
    except Exception:
        return False


def status() -> dict:
    out = {"version": active_version(), "topics": 0, "labels": 0, "reviews": 0,
           "assigned_at": _state_get(_ASSIGNED)}
    try:
        with db.session() as s:
            out["topics"] = int(s.execute(text(
                "SELECT count(*) FROM review_topic_def WHERE version = :v"),
                {"v": out["version"]}).scalar() or 0)
            out["labels"] = int(s.execute(text(
                "SELECT count(*) FROM review_topic_label")).scalar() or 0)
            out["reviews"] = int(s.execute(text(
                "SELECT count(DISTINCT url) FROM review_topic_label"
                " WHERE z >= :m AND rn <= :r"), {"m": MIN_Z, "r": RANK_CAP}).scalar() or 0)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out
