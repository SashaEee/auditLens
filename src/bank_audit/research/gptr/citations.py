"""Якоря цитат: от [f:12] писателя к [3] в отчёте и приложении.

ЗАЧЕМ. В прежнем отчёте приложение «Источники [1]–[44]» никак не связано с
текстом: нумерация декоративная, по номеру нельзя дойти до утверждения, а
счётчик цитирований на титуле искал маркеры [N] регулярным выражением и
показывал ноль. Здесь связь становится настоящей: писатель ссылается на
идентификатор ФАКТА, а не на источник, и после генерации якоря
перенумеровываются в номера источников — но уже по тому, что реально
процитировано.

Побочный, но важный эффект: источник, на который не сослались, в приложение не
попадает. Список перестаёт раздуваться страницами, которые собрали и не
использовали.
"""
from __future__ import annotations

import re

_ANCHOR_RE = re.compile(r"[\[(]f:(\d{1,5})[\])]")   # модель иногда пишет (f:12) — тоже якорь
_DUP_RE = re.compile(r"(\[\d{1,3}\])(?:\s?\1)+")      # [3][3] — один и тот же источник подряд


def renumber(report: str, registry) -> tuple[str, list[dict], dict]:
    """[f:id] → [N] по порядку появления в тексте.

    Возвращает (текст, источники_для_приложения, статистика). В приложение
    попадают только процитированные источники, в порядке первого упоминания.
    """
    by_id = {f.id: f for f in registry.facts}
    order: list[str] = []            # url в порядке первого цитирования
    facts_by_url: dict[str, list] = {}
    unknown = 0

    def repl(m: re.Match) -> str:
        nonlocal unknown
        fid = int(m.group(1))
        fact = by_id.get(fid)
        if fact is None:
            unknown += 1
            return ""                # якорь на несуществующий факт — убираем
        if fact.url not in order:
            order.append(fact.url)
            facts_by_url[fact.url] = []
        if fact not in facts_by_url[fact.url]:
            facts_by_url[fact.url].append(fact)
        return f"[{order.index(fact.url) + 1}]"

    text = _ANCHOR_RE.sub(repl, report or "")
    # Схлопываем соседние якоря: «[1] [1]» и «[1][2][1]» читаются плохо.
    text = re.sub(r"(?:\[(\d+)\])(?:\s*\[\1\])+", r"[\1]", text)

    sources = []
    for i, url in enumerate(order, 1):
        facts = facts_by_url[url]
        sources.append({
            "n": i, "url": url,
            "facts": [f.to_ui() for f in facts],
            "stance": facts[0].stance if facts else "",
            "cited": len(facts),
        })
    cited_ids = {int(m) for m in _ANCHOR_RE.findall(report or "")}
    return text, sources, {
        "цитирований": len(_ANCHOR_RE.findall(report or "")) - unknown,
        "фактов_процитировано": len(cited_ids & set(by_id)),
        "фактов_всего": len(by_id),
        "источников_процитировано": len(order),
        "якорей_в_никуда": unknown,
    }


class StreamRenumberer:
    """Перенумеровывает якоря ПО ХОДУ генерации отчёта.

    Отчёт писался целиком и приезжал в интерфейс одним куском: последние
    полторы минуты аудитор смотрел на «пишу по фактам». Стримить мешало то,
    что якоря [f:id] превращаются в номера источников только после генерации.
    Но нумерация идёт по порядку ПЕРВОГО УПОМИНАНИЯ — а это и есть порядок
    стрима, поэтому замену можно делать на лету.

    Единственная тонкость: якорь может разорваться между кусками потока, и
    незакрытый хвост придерживается до следующего куска.
    """

    def __init__(self, registry):
        self._by_id = {f.id: f for f in registry.facts}
        self._order: list[str] = []
        self._facts_by_url: dict[str, list] = {}
        self._buf = ""
        self._full: list[str] = []
        self.unknown = 0
        self.anchors = 0

    def _sub(self, m: re.Match) -> str:
        fid = int(m.group(1))
        fact = self._by_id.get(fid)
        if fact is None:
            self.unknown += 1
            return ""
        self.anchors += 1
        if fact.url not in self._order:
            self._order.append(fact.url)
            self._facts_by_url[fact.url] = []
        if fact not in self._facts_by_url[fact.url]:
            self._facts_by_url[fact.url].append(fact)
        return f"[{self._order.index(fact.url) + 1}]"

    def feed(self, chunk: str) -> str:
        """Кусок потока → готовый к показу текст (может быть пустым)."""
        self._buf += chunk or ""
        out = _DUP_RE.sub(r"\1", _ANCHOR_RE.sub(self._sub, self._buf))
        # Придерживаем незавершённый якорь в хвосте — и завершённый тоже:
        # его двойник может прийти следующим куском, и склеить их можно
        # только вместе.
        tail = 0
        # Хвост: цепочка уже готовых якорей, за которой идёт незавершённый или
        # ещё один готовый — «[1][f:1» + «3]» иначе даёт [1][1].
        m = re.search(r"(?:\[\d{1,3}\]\s?)*(?:[\[(]f?:?\d{0,5}|\[\d{1,3}\])$", out)
        if m:
            tail = len(out) - m.start()
        ready, self._buf = (out[:len(out) - tail], out[len(out) - tail:]) if tail else (out, "")
        self._full.append(ready)
        return ready

    def finish(self) -> str:
        ready, self._buf = self._buf, ""
        self._full.append(ready)
        return ready

    @property
    def text(self) -> str:
        return "".join(self._full)

    def sources(self) -> list[dict]:
        out = []
        for i, url in enumerate(self._order, 1):
            facts = self._facts_by_url[url]
            out.append({"n": i, "url": url,
                        "facts": [f.to_ui() for f in facts],
                        "stance": facts[0].stance if facts else "",
                        "cited": len(facts)})
        return out

    def stats(self) -> dict:
        return {"цитирований": self.anchors,
                "фактов_процитировано": len({f.id for fs in
                                             self._facts_by_url.values()
                                             for f in fs}),
                "фактов_всего": len(self._by_id),
                "источников_процитировано": len(self._order),
                "якорей_в_никуда": self.unknown}


def unanchored_claims(report: str) -> int:
    """Абзацы с утверждениями, но без единого якоря.

    Грубая, зато честная метрика дисциплины писателя: абзац, который что-то
    утверждает и ни на что не ссылается, для аудита бесполезен.
    """
    n = 0
    for para in (report or "").split("\n\n"):
        p = para.strip()
        if len(p) < 120 or p.startswith(("#", "|", "-", "*", ">")):
            continue
        if not _ANCHOR_RE.search(p) and not re.search(r"\[\d+\]", p):
            n += 1
    return n
