"""Base Agent — универсальный function-calling loop для автономных агентов v2.

Каждый агент получает:
  • mission (что нужно собрать) — от Кондуктора
  • tool-набор (подмножество tools/) — специализация агента
  • bundle (куда писать находки)

Агент автономен: сам решает сколько итераций tool-use сделать, какие
инструменты звать, когда данных достаточно чтобы вернуть результат.

Это обобщение паттерна из analyst.py:905- (quick path с tools), но:
  • без стриминга текста пользователю (агент пишет в bundle, не в чат)
  • с эмитом прогресса в SSE (для UI: tool_call события)
  • с финальным structured-ответом (агент возвращает артефакты, не прозу)

Главный цикл:
  loop (max_iter):
    resp = LLM(messages + tools)
    if tool_calls: выполнить, добавить tool_results в messages
    else: парсим финальный structured ответ, выходим
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

from ...clock import today_anchor
from .knowledge_bundle import KnowledgeBundle

log = logging.getLogger(__name__)


def _is_model_unavailable(exc: Exception) -> bool:
    """«Холодный» отказ модели на эндпоинте cloud.ru: 400 invalid model ID /
    model not found для валидной (есть в каталоге) модели. Повод деградировать
    на сильную модель, НЕ повод ретраить тот же id (он «холодный»)."""
    msg = str(exc).lower()
    if "invalid model" in msg or "model not found" in msg or "unknown model" in msg:
        return True
    if "model" in msg and ("does not exist" in msg or "not available" in msg):
        return True
    return False


def _extract_urls_from_search_result(tool_result: str) -> list[str]:
    """URL'ы из результата web_search — для forced-read pending list.

    web_search возвращает {"query","results":[{title,url,snippet,...}],"count":N}.
    Берём первые 8 URL (хватит на подсказку модели). Используется анти-«паралич
    поиска»: копим URL'ы из результативных поисков, чтобы потом подсунуть их в
    жёстко-форсированный read_url."""
    try:
        data = json.loads(tool_result)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for r in (data.get("results") or [])[:8]:
        if not isinstance(r, dict):
            continue
        url = (r.get("url") or "").strip()
        if url.startswith("http"):
            out.append(url)
    return out


def _salvage_json(raw: str) -> str | None:
    """Из ОБРЕЗАННОГО/fenced JSON-объекта собрать парсящийся: снять ```fences,
    отрезать по последней завершённой записи (закрывающая скобка / запятая на
    глубине ≥1), добалансировать незакрытые [ и {. Лечит обрыв facts-массива при
    усечении финального извлечения по max_tokens → сохраняет ПОЛНЫЕ факты,
    отбрасывает лишь последний неполный."""
    import re as _re
    if not raw:
        return None
    t = raw.strip()
    t = _re.sub(r"^```(?:json)?\s*", "", t, flags=_re.IGNORECASE).strip()
    if t.endswith("```"):
        t = t[:-3].rstrip()
    start = t.find("{")
    if start < 0:
        return None
    depth = 0; in_str = False; esc = False; cut = -1
    for i in range(start, len(t)):
        ch = t[i]
        if esc:
            esc = False; continue
        if ch == "\\" and in_str:
            esc = True; continue
        if ch == '"':
            in_str = not in_str; continue
        if in_str:
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1; cut = i + 1
        elif ch == "," and depth >= 1:
            cut = i
    if cut < 0:
        return None
    body = t[start:cut].rstrip().rstrip(",")
    closers: list[str] = []
    in_str = False; esc = False
    for ch in body:
        if esc:
            esc = False; continue
        if ch == "\\" and in_str:
            esc = True; continue
        if ch == '"':
            in_str = not in_str; continue
        if in_str:
            continue
        if ch == "{":
            closers.append("}")
        elif ch == "[":
            closers.append("]")
        elif ch in "}]" and closers:
            closers.pop()
    tail = '"' if in_str else ""
    return body + tail + "".join(reversed(closers))


# Тип tool-функции: (args_dict, bundle) -> json_string
ToolFn = Callable[[dict, KnowledgeBundle], str]


@dataclass
class ToolSpec:
    """Описание tool для function-calling + ссылка на реализацию."""
    name: str
    description: str
    parameters: dict          # JSON-schema для function-calling
    fn: ToolFn


@dataclass
class AgentMission:
    """Задание агенту от Кондуктора."""
    agent_id: str             # "researcher", "reviews", "regulatory", ...
    goal: str                 # что собрать (человеческим языком)
    subjects: list[str] = field(default_factory=list)  # банки/объекты
    focus: str = ""           # узкий фокус («только тарифы», «только жалобы»)
    constraints: list[str] = field(default_factory=list)  # доп. ограничения
    context: str = ""         # что уже известно (от других агентов)


@dataclass
class AgentProgress:
    """Прогресс агента для SSE/UI."""
    agent_id: str
    n_tool_calls: int = 0
    tools_used: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def to_ui(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "n_tool_calls": self.n_tool_calls,
            "tools_used": list(dict.fromkeys(self.tools_used)),
            "notes": self.notes[-5:],
            "elapsed_s": round(time.time() - self.started_at, 1),
        }


class BaseAgent:
    """Базовый автономный агент с function-calling.

    Подклассы определяют:
      • TOOLS (список ToolSpec)
      • SYSTEM_PROMPT
      • _parse_final_answer(content, bundle) — как интерпретировать финал
    """

    SYSTEM_PROMPT = (
        "Ты — автономный исследовательский агент для аудиторской платформы. "
        "Твоя задача — собрать конкретные данные по заданию, используя инструменты. "
        "Действуй итеративно: ищи → читай → извлекай факты → если данных мало, ищи ещё. "
        "Все числа и факты — только из найденных источников, с их номерами [N]. "
        "Когда данных достаточно — верни структурированный результат."
    )

    TOOLS: list[ToolSpec] = []

    # Тир модели для тиринга (ускорение v2). "fast"=Haiku, "smart"=Sonnet.
    #   MODEL_TIER       — модель для ИТЕРАЦИЙ цикла (поиск/чтение/роутинг).
    #   FINAL_MODEL_TIER — модель для ФИНАЛЬНОГО извлечения структуры (None = как
    #                      MODEL_TIER). Researcher: loop=fast, final=smart.
    MODEL_TIER: str = "smart"
    FINAL_MODEL_TIER: str | None = None

    def __init__(self, client: AsyncOpenAI, model: str,
                  mission: AgentMission, bundle: KnowledgeBundle,
                  max_iterations: int = 8,
                  loop_model: str | None = None,
                  final_model: str | None = None,
                  smart_model: str | None = None,
                  emit=None) -> None:
        self.client = client
        self.model = model
        # emit(dict) — опциональный sync-callback живого статуса агента наружу
        # (оркестратор кладёт в очередь → SSE agent_tool_call). None = молчим.
        self._emit = emit
        # loop_model — для итераций (по умолчанию = model); final_model — для
        # финального извлечения (по умолчанию = loop_model). Оркестратор задаёт
        # их по тиру агента; при отсутствии — обратная совместимость (всё = model).
        self.loop_model = loop_model or model
        self.final_model = final_model or self.loop_model
        # smart_model — сильная модель для деградации, если быстрая (Haiku) вернёт
        # «холодный» 400/недоступна (эндпоинт cloud.ru так умеет). По умолчанию =
        # final_model (он smart у researcher; у pure-fast агентов = model, который
        # оркестратор передаёт = smart).
        self.smart_model = smart_model or self.final_model
        self.mission = mission
        self.bundle = bundle
        self.max_iterations = max_iterations
        self.progress = AgentProgress(agent_id=mission.agent_id)
        # URL'ы, накопленные из результативных web_search — для forced-read
        # (анти-«паралич поиска»: агент игнорит soft-пинки и крутит поиск →
        # форсируем read_url жёстким tool_choice, подсунув эти URL'ы).
        self._pending_read_urls: list[str] = []
        self._forced_read_done = False
        self._search_capped_notified = False
        self._read_capped_notified = False
        # Успешные чтения (вернули непустой текст). Потолок чтений считаем по НИМ,
        # а не по всем попыткам: SPA/404/captcha-страницы офиц.сайтов отдают пусто,
        # и раньше каждая такая попытка жгла слот из ~5 → банк оставался без данных.
        self._successful_reads = 0

    def _emit_status(self, **kw):
        """Живой статус агента наружу (если подключён emit). Никогда не роняет
        агента — телеметрия не должна влиять на сбор данных.

        ИНВАРИАНТ (важно): emit вызывается СИНХРОННО в event-loop потоке внутри
        run(). Дренаж очереди в оркестраторе (_run_missions_streaming) полагается
        на то, что каждый put_nowait происходит ДО завершения таски агента —
        иначе финальный слив очереди по empty() потеряет поздние события. НЕ
        переносить _emit в run_in_executor / fire-and-forget таску."""
        if not self._emit:
            return
        try:
            self._emit({"n_reads": self._successful_reads,
                        "calls": self.progress.n_tool_calls,
                        "model": (self.loop_model or "").split("/")[-1], **kw})
        except Exception:
            pass

    # ── main loop ──────────────────────────────────────────────────────
    async def run(self) -> dict:
        """Главный цикл агента. Возвращает dict с артефактами для bundle."""
        messages = self._build_messages()

        tools_schema = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self.TOOLS
        ]

        final_artifacts: dict = {}
        # Общий дедлайн агента: даже если эндпоинт попал в «медленное окно» и
        # вызовы упираются в wall-стену + ретраи, один агент НЕ должен держать всю
        # волну сбора минутами. По истечении — мягкий финал из уже собранного
        # (данные не теряются). Тюнится V2_AGENT_BUDGET_S.
        agent_started = time.time()
        agent_budget = float(os.getenv("V2_AGENT_BUDGET_S", "220"))
        for iteration in range(self.max_iterations):
            if time.time() - agent_started > agent_budget:
                log.warning("[agent:%s] бюджет времени %.0fs исчерпан (iter %s, "
                             "%s tool-вызовов) — мягкий финал из собранного",
                             self.mission.agent_id, agent_budget, iteration,
                             self.progress.n_tool_calls)
                if self.progress.n_tool_calls > 0 and not final_artifacts:
                    try:
                        final_artifacts = await self._extract_final(
                            messages, self.final_model)
                    except Exception as e:
                        log.warning("[agent:%s] финал по дедлайну упал: %s",
                                     self.mission.agent_id, e)
                break
            self._emit_status(phase="think", iter=iteration)  # «обдумывает ход»
            used = self.progress.tools_used
            n_search = sum(1 for t in used if t in ("web_search", "semantic_search"))
            n_read = self._successful_reads   # потолок — по УСПЕШНЫМ чтениям
            n_read_attempts = sum(1 for t in used if t == "read_url")
            n_subj = max(1, len(self.mission.subjects))

            # ── Жёсткий потолок web_search ───────────────────────────────────
            # Агенты игнорят «1-2 поиска» в промпте и наматывают 5-6 web_search до
            # max_iterations → главная утечка времени (поиск медленный, а даёт лишь
            # сниппеты — факты ТОЛЬКО из read_url, поэтому срезать лишние поиски
            # качество-нейтрально). После лимита убираем web_search из доступных
            # инструментов → агент обязан читать/финалить. Кэп ≈1 поиск на субъект
            # + 1 кросс-источник (env V2_WEB_SEARCH_CAP_MIN — нижняя граница).
            n_websearch = sum(1 for t in used if t == "web_search")
            search_cap = max(int(os.getenv("V2_WEB_SEARCH_CAP_MIN", "3")),
                              int(os.getenv("V2_WEB_SEARCH_PER_SUBJECT", "2")) * n_subj)
            iter_tools = tools_schema
            if n_websearch >= search_cap:
                iter_tools = [t for t in tools_schema
                              if t["function"]["name"] != "web_search"]
                if not self._search_capped_notified:
                    messages.append({"role": "user", "content":
                        f"Лимит web_search ({search_cap}) исчерпан — кандидатов "
                        "достаточно. БОЛЬШЕ НЕ ИЩИ. Прочитай (read_url) самые "
                        "релевантные найденные страницы и верни факты по заданию."})
                    self._search_capped_notified = True
                    log.warning("[agent:%s] web_search cap (%s) достигнут — "
                                 "отключаю поиск, форсирую чтение/финал",
                                 self.mission.agent_id, search_cap)

            # ── Жёсткий потолок read_url (гейт сходимости) ───────────────────
            # Корень лага: под нагрузкой агент мотает итерации, читая по одному —
            # история раздувается (~6КБ/чтение) → поздние nav-вызовы и финальная
            # Sonnet-экстракция тяжелеют и бьются о wall-стену. После read_cap
            # чтений убираем read_url+web_search → модель ОБЯЗАНА финалить из уже
            # прочитанного. Полезных чтений в селективном режиме ~3-5 (SPA-страницы
            # офиц.сайтов всё равно пустые) → качество-нейтрально.
            read_cap = min(int(os.getenv("V2_READ_URL_CAP_MAX", "12")),
                           max(int(os.getenv("V2_READ_URL_CAP", "5")),
                               int(os.getenv("V2_READ_PER_SUBJECT", "4")) * n_subj))
            # Жёсткий потолок ПОПЫТОК (вкл. пустые) — чтобы не зациклиться на пустых
            # SPA, если успешных чтений всё не набирается.
            read_attempt_ceiling = read_cap * 2 + 2
            if n_read >= read_cap or n_read_attempts >= read_attempt_ceiling:
                iter_tools = [t for t in iter_tools
                              if t["function"]["name"] not in ("read_url", "web_search")]
                if not self._read_capped_notified:
                    messages.append({"role": "user", "content":
                        f"Прочитано достаточно ({n_read} страниц) — кандидатов хватает. "
                        "СТОП собирать. Верни ИТОГОВЫЙ JSON с фактами по заданию из "
                        "уже прочитанного (числа/цитаты со ссылками [N])."})
                    self._read_capped_notified = True
                    log.warning("[agent:%s] read_url cap (%s) достигнут — "
                                 "форсирую финал из собранного",
                                 self.mission.agent_id, read_cap)

            # ── Анти-«паралич поиска» (жёсткий forced-read) ───────────────────
            # Баг A/B: агент (особенно Haiku) крутит web_search до max_iterations,
            # игнорируя soft-пинки в тексте, и ни разу не зовёт read_url → 0 фактов,
            # т.к. сниппеты SERP'а — НЕ факты. Лечение: после ≥2 поисков с непустыми
            # результатами и НИ ОДНОГО чтения — ЖЁСТКО отбираем у модели выбор через
            # tool_choice=dict (обязана позвать read_url), подсунув накопленные URL'ы.
            # forced-read срабатывает один раз; если он не помог (модель всё равно не
            # прочла) — больше не зажимаем, чтобы не блокировать агентский цикл.
            force_tool = None
            if (not self._forced_read_done and n_read == 0
                    and self._pending_read_urls
                    and iteration < self.max_iterations - 1
                    and (n_search >= 2 or iteration >= 3)):
                url_hint = "\n".join(f"- {u}" for u in self._pending_read_urls[:6])
                messages.append({"role": "user", "content":
                    "СТОП искать. Ты сделал поиск и получил URL'ы, но не прочитал "
                    "НИ ОДНОЙ страницы — факты из сниппетов извлекать НЕЛЬЗЯ. СЕЙЧАС "
                    "вызови read_url на 2-3 САМЫХ релевантных к заданию URL (по "
                    "теме и нужному объекту); нерелевантные пропусти. Кандидаты:\n"
                    + url_hint})
                force_tool = "read_url"
                self._forced_read_done = True
                log.warning("[agent:%s] forced read_url triggered (iter %s, "
                             "%s searches, %s pending urls)",
                             self.mission.agent_id, iteration, n_search,
                             len(self._pending_read_urls))

            try:
                resp = await self._call_llm(messages, iter_tools,
                                             force_tool=force_tool)
            except Exception as e:
                # Сбой навигационного вызова — чаще всего wall-таймаут в «плохом
                # окне» эндпоинта (после кэпа ретраев в throttle). НЕ теряем уже
                # прочитанные страницы: если что-то собрано — принудительно
                # извлекаем факты из накопленной истории (сильной моделью). Без
                # этого break минует финал-ветку → final_artifacts={} → 0 фактов,
                # хотя read_url успел отработать. Грейсфул-деградация > потеря.
                log.warning("[agent:%s] LLM call failed (iter %s): %s%s",
                             self.mission.agent_id, iteration, e,
                             " — грейсфул-финал из собранного"
                             if self.progress.n_tool_calls else "")
                if self.progress.n_tool_calls > 0 and not final_artifacts:
                    try:
                        final_artifacts = await self._extract_final(
                            messages, self.final_model)
                    except Exception as e2:
                        log.warning("[agent:%s] грейсфул-финал тоже упал: %s",
                                     self.mission.agent_id, e2)
                break

            choice = resp.choices[0]
            msg = choice.message
            tool_calls = getattr(msg, "tool_calls", None) or []

            # Добавляем ответ ассистента в историю
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                   "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ] or None,
            })

            if not tool_calls:
                # Финал без чтения, хотя forced-read ещё не пробовали и URL'ы есть —
                # один раз форсируем чтение, не принимая этот финал.
                if (not self._forced_read_done and n_read == 0
                        and self._pending_read_urls
                        and iteration < self.max_iterations - 1):
                    continue  # след. итерация войдёт в ветку forced-read выше
                # Финальное извлечение. Если цикл шёл на быстрой модели (loop≠final) —
                # переизвлекаем структуру СИЛЬНОЙ моделью из всего собранного (она
                # видит прочитанные страницы в истории). Иначе берём контент как есть.
                if self.final_model != self.loop_model:
                    try:
                        final_artifacts = await self._extract_final(
                            messages, self.final_model)
                    except Exception as e:
                        log.warning("[agent:%s] final-extract на %s упал: %s — беру loop-финал",
                                     self.mission.agent_id, self.final_model, e)
                        final_artifacts = await self._extract_final(
                            messages, self.loop_model, seed_content=msg.content)
                else:
                    # final==loop (напр. reviews/regulatory на Haiku): парсим финал
                    # loop-модели, а при не-JSON — строгий перезапрос (фикс reviews).
                    final_artifacts = await self._extract_final(
                        messages, self.loop_model, seed_content=msg.content)
                self.progress.notes.append(
                    f"Финал: {final_artifacts.get('summary', 'готово')[:80]}")
                break

            # Выполняем tool calls ПАРАЛЛЕЛЬНО. Раньше шли строго последовательно
            # (for tc: await _exec_tool) → read_url на 3-6 URL (которые форсирует
            # сам forced-read) исполнялись по одному = утечка. Теперь gather; порядок
            # tool-сообщений в истории сохраняем по позиции (требование OpenAI-протокола).
            async def _run_tc(tc):
                args = self._safe_json(tc.function.arguments)
                return await self._exec_tool(tc.function.name, args)
            results = await asyncio.gather(*[_run_tc(tc) for tc in tool_calls],
                                           return_exceptions=False)
            for tc, result in zip(tool_calls, results):
                tool_name = tc.function.name
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tool_name,
                    "content": result,
                })
                self.progress.n_tool_calls += 1
                self.progress.tools_used.append(tool_name)
                # Успешное чтение = read_url без "error" и с непустым text.
                if (tool_name == "read_url" and '"error"' not in result[:120]
                        and '"text"' in result):
                    self._successful_reads += 1
                # Живой статус наружу: какой инструмент только что отработал.
                self._emit_status(tool=tool_name)
                # Копим URL'ы из результативных web_search — для forced-read.
                if tool_name == "web_search":
                    for u in _extract_urls_from_search_result(result):
                        if u not in self._pending_read_urls:
                            self._pending_read_urls.append(u)
        else:
            log.warning("[agent:%s] max_iterations reached (%s) — принудительный финал",
                         self.mission.agent_id, self.max_iterations)
            # Последняя попытка: попросить финал без tools (на СИЛЬНОЙ модели).
            try:
                final_artifacts = await self._extract_final(messages, self.final_model)
            except Exception:
                pass

        # ── Фикс #3: extraction-gap ≠ data-absence ───────────────────────────
        # Если структурные данные НЕ извлечены, но источники прочитаны — это сбой
        # ИЗВЛЕЧЕНИЯ, а не отсутствие данных у банка. Помечаем честно отдельной
        # coverage-заметкой, чтобы отчёт не выдавал «банк не раскрыл» за факт.
        n_read_final = sum(1 for t in self.progress.tools_used if t == "read_url")
        _data_keys = ("facts", "complaints", "praise", "regulations",
                      "sentiment_profiles", "insights")
        _got_data = any(isinstance(final_artifacts.get(k), list)
                        and final_artifacts.get(k) for k in _data_keys)
        if not _got_data and n_read_final > 0:
            from .knowledge_bundle import CoverageNote
            self.bundle.coverage_notes.append(CoverageNote(
                what=f"извлечение не удалось: прочитано {n_read_final} источн., "
                     "структурные данные не извлечены",
                subjects=list(self.mission.subjects),
                reason="extraction_gap (сбой парсинга/формата, НЕ отсутствие данных у банка)",
                recommendation="перезапросить извлечение строгим JSON / проверить первоисточник вручную"))
            log.warning("[agent:%s] extraction-gap: %d источн. прочитано, 0 данных",
                         self.mission.agent_id, n_read_final)

        # Подклассы могут постпроцессить артефакты → bundle
        await self._integrate(final_artifacts)
        return {"agent_id": self.mission.agent_id,
                "progress": self.progress.to_ui(),
                "artifacts": final_artifacts}

    # ── helpers ────────────────────────────────────────────────────────
    def _build_messages(self) -> list[dict]:
        _year = datetime.now().year
        user = (
            f"# ЗАДАНИЕ\n{self.mission.goal}\n\n"
            f"# ОБЪЕКТЫ\n{', '.join(self.mission.subjects) or '(не заданы)'}\n"
        )
        if self.mission.focus:
            user += f"\n# ФОКУС\n{self.mission.focus}\n"
        if self.mission.constraints:
            user += "\n# ОГРАНИЧЕНИЯ\n- " + "\n- ".join(self.mission.constraints)
        if self.mission.context:
            user += f"\n\n# УЖЕ ИЗВЕСТНО (от других агентов)\n{self.mission.context}"
        user += (
            "\n\n# ИНСТРУКЦИЯ (СЕЛЕКТИВНО — скорость и качество = меньше шума)\n"
            "1. Начни с semantic_search (быстро, бесплатно) — данные могут быть в кэше.\n"
            "2. Сделай 1-2 web_search МАКСИМУМ. НЕ перебирай 5-6 поисков — после\n"
            "   1-2 у тебя уже есть кандидаты.\n"
            "3. По title+snippet ОТБЕРИ 2-4 САМЫХ релевантных URL и прочитай ТОЛЬКО\n"
            "   их (read_url). Нерелевантное (не про тему/не про этот объект) — НЕ\n"
            "   читай, не трать время. Поиск даёт лишь сниппеты — факты ТОЛЬКО из\n"
            "   прочитанных страниц.\n"
            "4. Числа/факты бери ТОЛЬКО из прочитанного (read_url), с номером [N].\n"
            "5. Если по объекту данных нет в открытых источниках — честно скажи,\n"
            "   не ищи бесконечно.\n"
            "6. Когда прочитал релевантные страницы и собрал факты — верни JSON.\n"
            f"7. СВЕЖЕСТЬ (критично для аудита): добавляй текущий год в запросы "
            f"(напр. «тарифы {_year}», «условия {_year}»), предпочитай свежие "
            f"страницы. Для КАЖДОГО факта ОБЯЗАТЕЛЬНО заполни as_of (год/период из "
            f"источника). Данные старше {_year - 2} — слабый источник: ищи актуальнее "
            f"или явно помечай как устаревшие."
        )
        return [{"role": "system", "content": today_anchor() + "\n\n" + self.SYSTEM_PROMPT},
                {"role": "user", "content": user}]

    async def _call_llm(self, messages, tools_schema, force_final=False,
                         model=None, force_tool: str | None = None):
        """Один вызов LLM в цикле агента.

        force_tool — имя tool, который модель ОБЯЗАНА вызвать (dict-форма
        tool_choice). Используется анти-«паралич поиска»: после результативных
        поисков жёстко отбираем у модели выбор — обязана позвать read_url.
        Safety: если эндпоинт (cloud.ru) не принял dict-форму — fallback на
        "auto" (поведение деградирует до soft-режима, не хуже текущего)."""
        use_model = model or self.loop_model
        kwargs: dict = {
            "model": use_model,
            "messages": messages,
            "temperature": 0.0,
        }
        if tools_schema and not force_final:
            kwargs["tools"] = tools_schema
            # dict-форма tool_choice ЖЁСТКО требует конкретный tool. Безопасна:
            # проверяем, что имя в schema; иначе эндпоинт вернёт 400.
            if force_tool and any(t["function"]["name"] == force_tool
                                   for t in tools_schema):
                kwargs["tool_choice"] = {"type": "function",
                                          "function": {"name": force_tool}}
            else:
                kwargs["tool_choice"] = "auto"
        else:
            # force_final = извлечение фактов из ВСЕХ прочитанных страниц → JSON
            # может быть большим. 4000 обрезало его на середине → fence не закрыт →
            # parse fail → 0 фактов. Даём запас (эндпоинт не поддерживает
            # response_format=json_object, поэтому надёжность — через объём + salvage).
            kwargs["max_tokens"] = 8000 if force_final else 4000
            if force_final:
                kwargs["messages"] = messages + [{
                    "role": "user",
                    "content": "Верни ТОЛЬКО валидный JSON-объект по схеме из задания "
                               "(БЕЗ markdown-преамбулы, можно в ```json блоке). "
                               "Все собранные факты — в массиве facts. Не вызывай инструменты."
                }]
        try:
            return await self.client.chat.completions.create(**kwargs)
        except Exception as e:
            # Forced-tool dict-форма может быть не поддержана эндпоинтом (cloud.ru
            # OpenAI-совместим, но нет гарантии). При ошибке — один fallback на
            # "auto" (soft-режим): лучше пусть модель сама решит, чем потерять ход.
            if force_tool and "tool_choice" in kwargs and isinstance(
                    kwargs["tool_choice"], dict):
                log.warning("[agent:%s] tool_choice=dict (%s) отклонён эндпоинтом "
                             "(%s) — fallback на auto",
                             self.mission.agent_id, force_tool, str(e)[:80])
                kwargs["tool_choice"] = "auto"
                try:
                    return await self.client.chat.completions.create(**kwargs)
                except Exception:
                    pass  # падаем в общую обработку ниже
            # Эндпоинт иногда отдаёт «холодный» 400/invalid model ID для ВАЛИДНОЙ
            # быстрой модели. Деградируем на сильную — лучше медленнее, чем потерять
            # агента (и замер тиринга не сорвётся от хиккапа Haiku).
            if use_model != self.smart_model and _is_model_unavailable(e):
                log.warning("[agent:%s] модель %s недоступна (%s) — фоллбэк на %s",
                             self.mission.agent_id, use_model.split("/")[-1],
                             str(e)[:60], self.smart_model.split("/")[-1])
                kwargs["model"] = self.smart_model
                return await self.client.chat.completions.create(**kwargs)
            raise

    async def _exec_tool(self, name: str, args: dict) -> str:
        """Выполняет tool (в executor — т.к. tools синхронные с I/O)."""
        tool = next((t for t in self.TOOLS if t.name == name), None)
        if tool is None:
            return json.dumps({"error": f"unknown tool {name}"})
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, tool.fn, args, self.bundle)
            # Обрезаем длинные tool-результаты (контекст LLM). Для read_url —
            # потолок выше (18000): финальное извлечение фактов на сильной модели
            # (FINAL_MODEL_TIER=smart у researcher) идёт из истории сообщений, и
            # потерянный кусок страницы = потерянный факт. Sonnet имеет 200k
            # контекста — запас есть. web_search/semantic_search остаются 12000.
            cap = 18000 if name == "read_url" else 12000
            if len(result) > cap:
                result = result[:cap] + "\n…[обрезано]…"
            return result
        except Exception as e:
            log.warning("[agent:%s] tool %s failed: %s",
                         self.mission.agent_id, name, e)
            return json.dumps({"error": f"{name} failed: {e}"},
                              ensure_ascii=False)

    def _safe_json(self, s: str) -> dict:
        if not s:
            return {}
        try:
            return json.loads(s)
        except Exception:
            from ...ai.llm_utils import _loose_json_loads
            try:
                data = _loose_json_loads(s)
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}

    def _safe_parse(self, content: str) -> dict:
        """Парсит финальный ответ агента как JSON."""
        if not content:
            return {}
        # Сначала пробуем прямой/loose JSON
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        from ...ai.llm_utils import _loose_json_loads
        try:
            data = _loose_json_loads(content)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        # Salvage обрезанного JSON (финал извлечения из 8 страниц мог упереться в
        # max_tokens и оборваться) — восстанавливаем полные факты, дропая неполный.
        salv = _salvage_json(content)
        if salv:
            try:
                data = json.loads(salv)
                if isinstance(data, dict):
                    log.warning("[agent:%s] финал восстановлен salvage (обрезанный JSON, %d ключей)",
                                 self.mission.agent_id, len(data))
                    return data
            except Exception:
                pass
        # Возможно LLM дала markdown с объяснением без JSON — сохраняем как summary
        log.warning("[agent:%s] финал НЕ распознан как JSON (%d симв) — summary-фоллбэк",
                     self.mission.agent_id, len(content))
        return {"summary": content.strip()[:1000],
                "_note": "agent did not return JSON, raw content captured"}

    async def _extract_final(self, messages, model, seed_content: str | None = None) -> dict:
        """Финальное извлечение с РЕТРАЕМ при парс-фейле.

        seed_content задан (final==loop) → парсим уже готовый финальный текст
        loop-модели; иначе делаем force_final-вызов на model. Если парс дал
        summary-фоллбэк (ответ не-JSON, помечен '_note') — ОДИН строгий
        перезапрос «верни строго JSON». Баг, который чиним: reviews выдал
        10397 симв не-JSON → 0 структурных жалоб (данные были, потерялись)."""
        if seed_content is not None:
            arts = self._safe_parse(seed_content)
        else:
            fresp = await self._call_llm(messages, [], force_final=True, model=model)
            arts = self._safe_parse(fresp.choices[0].message.content or "")
        if not arts.get("_note"):
            return arts
        log.warning("[agent:%s] финал не-JSON → строгий перезапрос (model=%s)",
                     self.mission.agent_id, str(model).split("/")[-1])
        strict = messages + [{"role": "user", "content":
            "Прошлый ответ НЕ был валидным JSON — данные потеряны. Верни СТРОГО "
            "валидный JSON-объект ПО СХЕМЕ ЗАДАНИЯ и БОЛЬШЕ НИЧЕГО: начни с '{', "
            "закончи '}', без markdown, без пояснений до или после."}]
        try:
            r2 = await self._call_llm(strict, [], force_final=True, model=model)
            arts2 = self._safe_parse(r2.choices[0].message.content or "")
            if not arts2.get("_note"):
                return arts2
        except Exception as e:
            log.warning("[agent:%s] строгий перезапрос финала упал: %s",
                         self.mission.agent_id, e)
        return arts

    async def _integrate(self, artifacts: dict) -> None:
        """Хук для подклассов: преобразовать артефакты → bundle.
        Базовая реализация ничего не делает (агент только собрал данные).
        Подклассы (Researcher, Reviews) переопределяют."""
        pass
