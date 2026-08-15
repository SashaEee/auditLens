"""Проверки волны 2: подмена предмета вопроса и честные пробелы.

Критик — единственное место, где система может заметить, что отчёт отвечает не
на тот вопрос. Раньше такой отчёт спокойно доезжал до аудитора: замечания
критика уходили в директиву ремонта и там растворялись, а пробелы, которые
переписыванием не закрыть (данных нет в источниках), не показывались вообще.

Запуск:  .venv/bin/python scripts/_test_critic_subject.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bank_audit.research.v2.critic import critique_report  # noqa: E402
from bank_audit.research.v2.knowledge_bundle import KnowledgeBundle, Fact  # noqa: E402

ok = fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  ✓ {name}")
    else:
        fail += 1
        print(f"  ✗ {name}\n      получено: {got!r}\n      ожидалось: {want!r}")


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class FakeLLM:
    """Отдаёт заранее заданный JSON вместо похода в модель."""

    def __init__(self, payload):
        self._raw = json.dumps(payload, ensure_ascii=False)
        self.chat = self
        self.completions = self

    async def create(self, **kw):
        return _Resp(self._raw)


def _bundle():
    b = KnowledgeBundle()
    b.facts = [Fact(subject="cbr", attribute="ПСК", value="27,608%",
                    source_n=1, verbatim="среднерыночное 27,608% годовых")]
    return b


REPORT = ("# Ставки по кредитам наличными\n\n## TL;DR\n"
          + "Сбер против рынка по витринам банков. " * 20)


def run(payload):
    return asyncio.run(critique_report(FakeLLM(payload), REPORT, _bundle(),
                                       "Предельные значения ПСК ЦБ на II квартал"))


print("подмена предмета вопроса")
c = run({"ok": True, "subject_mismatch": "спросили таблицу ПСК ЦБ, отчёт про ставки банков",
         "unanswered": [], "blocking_issues": [], "weak_claims": [],
         "missing_aspects": [], "numeric_hallucinations": [], "citation_errors": [],
         "repair_directive": ""})
check("подмена ловится", c.subject_mismatch,
      "спросили таблицу ПСК ЦБ, отчёт про ставки банков")
check("«ok»: true от модели не спасает отчёт с подменой", c.ok, False)
check("директива ремонта появляется даже при пустой директиве модели",
      c.repair_directive.startswith("ГЛАВНОЕ: отчёт отвечает не на тот вопрос"), True)
check("директива велит вернуться к вопросу, а не косметику наводить",
      "Перестрой отчёт вокруг заданного вопроса" in c.repair_directive, True)

print("подмена вперёд остальных замечаний")
c = run({"ok": False, "subject_mismatch": "спросили X, отчёт про Y", "unanswered": [],
         "blocking_issues": ["нет рейтинга"], "weak_claims": [], "missing_aspects": [],
         "numeric_hallucinations": [], "citation_errors": [],
         "repair_directive": "Добавь рейтинг."})
check("своя директива модели сохранена", "Добавь рейтинг." in c.repair_directive, True)
check("но предмет идёт первым",
      c.repair_directive.index("ГЛАВНОЕ") < c.repair_directive.index("Добавь рейтинг."), True)

print("честные пробелы")
c = run({"ok": False, "subject_mismatch": "", "blocking_issues": [],
         "unanswered": ["предельные значения ПСК на II квартал 2026 — в источниках нет",
                        "  ", "дата публикации документа"],
         "weak_claims": [], "missing_aspects": [], "numeric_hallucinations": [],
         "citation_errors": [], "repair_directive": ""})
check("пробелы разобраны", len(c.unanswered), 2)
check("пустые строки отброшены", "  " in c.unanswered, False)
check("текст пробела сохранён дословно", c.unanswered[0],
      "предельные значения ПСК на II квартал 2026 — в источниках нет")
check("пробел сам по себе не дописывает директиву ремонта",
      c.repair_directive, "")

print("чистый отчёт")
c = run({"ok": True, "subject_mismatch": "", "unanswered": [], "blocking_issues": [],
         "weak_claims": [], "missing_aspects": [], "numeric_hallucinations": [],
         "citation_errors": [], "repair_directive": ""})
check("хороший отчёт проходит", c.ok, True)
check("ни подмены", c.subject_mismatch, "")
check("ни пробелов", c.unanswered, [])

print("устойчивость к молчанию модели о новых полях")
c = run({"ok": True, "blocking_issues": [], "weak_claims": [], "missing_aspects": [],
         "numeric_hallucinations": [], "citation_errors": [], "repair_directive": ""})
check("отсутствие полей не роняет разбор", (c.ok, c.subject_mismatch, c.unanswered),
      (True, "", []))

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
