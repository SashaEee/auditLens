/* loophole.jsx — модуль loophole: левый sidebar-чат (AI-agent стиль) +
   основная область с таблицей найденных лазеек из БД, фильтрами и CSV-экспортом. */
const { useState, useEffect, useRef, useCallback, useMemo } = React;

const API = "/api/loophole";

// Константы фаз пайплайна (без финальной "done" в progress-bar).
// Только фазы, которые РЕАЛЬНО шлёт nanobot-бэкенд (stream_chat): clarify →
// execute → answer. Старые plan/aggregate остались от удалённого ReAct-графа и
// висели в степпере как фантомные непройденные шаги.
const PHASES = ["clarify", "execute", "answer"];

function LoopholeApp() {
  // ── Таблица / фильтры ──────────────────────────────────────────────────────
  const [records, setRecords] = useState([]);
  const [loading, setLoading] = useState(false);
  const [bankOptions, setBankOptions] = useState([]);
  // Фильтры
  const [fText, setFText] = useState("");
  const [fBanks, setFBanks] = useState([]);          // выбранные slug
  const [fFrom, setFFrom] = useState("");
  const [fTo, setFTo] = useState("");
  const [fVerdict, setFVerdict] = useState("all");   // all | loophole | not | null
  const [fStatus, setFStatus] = useState("");
  // Сортировка
  const [sortKey, setSortKey] = useState("verdict_confidence");
  const [sortDir, setSortDir] = useState("desc");
  // Выделение строк
  const [selected, setSelected] = useState(new Set());

  // ── Чат ────────────────────────────────────────────────────────────────────
  const [chat, setChat] = useState([]);
  const [chatInput, setChatInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);
  const [workspaceId, setWorkspaceId] = useState(null);
  const chatScrollRef = useRef(null);

  // ── Новый пайплайн: фазы / подзадачи / уточняющие вопросы ────────────────
  const [phase, setPhase] = useState(null);                // текущая фаза
  const [subtasks, setSubtasks] = useState([]);            // [{title, status}]
  const [pendingQuestions, setPendingQuestions] = useState(null); // null | array
  const [pendingQuery, setPendingQuery] = useState("");           // исходный запрос, вызвавший clarify
  const [answersByQ, setAnswersByQ] = useState({});        // {qid: {selected:[], other:""}}
  const [toolEvents, setToolEvents] = useState([]);        // badges tool_call/tool_result

  // ── Парсеры ───────────────────────────────────────────────────────────────
  const [parsersOpen, setParsersOpen] = useState(false);
  const [parsers, setParsers] = useState([]);
  const [newParserQuery, setNewParserQuery] = useState("");
  const [parsersBusy, setParsersBusy] = useState(false);

  // Создаём workspace при старте.
  useEffect(() => {
    fetch(`${API}/workspace`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: "default"}),
    })
      .then(r => r.json())
      .then(d => setWorkspaceId(d.workspace_id))
      .catch(() => {});
  }, []);

  // Загружаем список банков для фильтра.
  useEffect(() => {
    fetch(`${API}/banks`).then(r => r.json()).then(d => {
      setBankOptions(d.banks || []);
    }).catch(() => {});
  }, []);

  // Загружаем записи.
  const loadRecords = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (fText.trim()) params.set("q", fText.trim());
      if (fBanks.length) params.set("bank_slugs", fBanks.join(","));
      if (fFrom) params.set("period_from", fFrom);
      if (fTo) params.set("period_to", fTo);
      if (fVerdict === "loophole") params.set("only_loophole", "true");
      else if (fVerdict === "not") params.set("only_loophole", "false");
      if (fStatus) params.set("status", fStatus);
      const url = `${API}/records${params.toString() ? "?" + params.toString() : ""}`;
      const r = await fetch(url);
      const d = await r.json();
      setRecords(d.records || []);
    } finally {
      setLoading(false);
    }
  }, [fText, fBanks, fFrom, fTo, fVerdict, fStatus]);

  useEffect(() => { loadRecords(); }, [loadRecords]);

  // Сброс выделения при смене фильтров.
  useEffect(() => { setSelected(new Set()); }, [fText, fBanks, fFrom, fTo, fVerdict, fStatus]);

  // ── Сортировка на клиенте ──────────────────────────────────────────────────
  const sortedRecords = useMemo(() => {
    const arr = [...records];
    const dir = sortDir === "asc" ? 1 : -1;
    arr.sort((a, b) => {
      let va = a[sortKey], vb = b[sortKey];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === "string") return va.localeCompare(vb) * dir;
      return (Number(va) - Number(vb)) * dir;
    });
    return arr;
  }, [records, sortKey, sortDir]);

  const toggleSort = (key) => {
    if (sortKey === key) {
      setSortDir(d => d === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  const toggleRow = (id) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    if (selected.size === sortedRecords.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(sortedRecords.map(r => r.record_id)));
    }
  };

  // ── CSV-экспорт по фильтрам ────────────────────────────────────────────────
  const exportCSV = useCallback(async () => {
    const r = await fetch(`${API}/export/csv`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        bank_slugs: fBanks,
        period_from: fFrom || null,
        period_to: fTo || null,
        query_text: fText.trim(),
        only_loophole: fVerdict === "loophole" ? true
                     : fVerdict === "not" ? false : null,
        status: fStatus || null,
      }),
    });
    const blob = new Blob([await r.text()], {type: "text/csv;charset=utf-8"});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "loopholes.csv"; a.click();
    URL.revokeObjectURL(url);
  }, [fText, fBanks, fFrom, fTo, fVerdict, fStatus]);

  // ── Парсеры: список + CRUD + polling ───────────────────────────────────────
  const loadParsers = useCallback(async () => {
    if (!workspaceId) return;
    try {
      const r = await fetch(`${API}/parsers?workspace_id=${workspaceId}`);
      const d = await r.json();
      setParsers(d.parsers || []);
    } catch {}
  }, [workspaceId]);

  useEffect(() => {
    if (!parsersOpen) return;
    loadParsers();
    const t = setInterval(loadParsers, 5000);
    return () => clearInterval(t);
  }, [parsersOpen, loadParsers]);

  const createParser = async () => {
    if (!newParserQuery.trim() || !workspaceId) return;
    setParsersBusy(true);
    try {
      const r = await fetch(`${API}/parsers`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({workspace_id: workspaceId, query: newParserQuery.trim()}),
      });
      const d = await r.json();
      setNewParserQuery("");
      await loadParsers();
      return d;
    } finally {
      setParsersBusy(false);
    }
  };

  const startParser = async (pid) => {
    setParsersBusy(true);
    try {
      await fetch(`${API}/parsers/${pid}/run`, {method: "POST"});
      await loadParsers();
    } finally {
      setParsersBusy(false);
    }
  };

  const stopParser = async (pid) => {
    setParsersBusy(true);
    try {
      await fetch(`${API}/parsers/${pid}/stop`, {method: "POST"});
      await loadParsers();
    } finally {
      setParsersBusy(false);
    }
  };

  const statusParser = async (pid) => {
    try {
      const r = await fetch(`${API}/parsers/${pid}/status`);
      return await r.json();
    } catch (e) { return null; }
  };

  // ── Чат: отправка + полный SSE-парсер ──────────────────────────────────────
  const sendChat = useCallback(async (overrideMessage, opts) => {
    const skipClarify = !!(opts && opts.skipClarify);
    const userMsg = overrideMessage != null ? overrideMessage : chatInput;
    if (!userMsg || !userMsg.trim() || !workspaceId) return;
    // запоминаем ИСХОДНЫЙ запрос (не enriched) — из него build_enriched_question
    // соберёт обогащённый вопрос после ответов на уточнения
    if (!skipClarify) setPendingQuery(userMsg);
    setChat(prev => [...prev, {role: "user", content: userMsg}]);
    if (overrideMessage == null) setChatInput("");
    setChatLoading(true);
    setToolEvents([]);
    setPendingQuestions(null);
    let gotQuestions = false;
    try {
      const resp = await fetch(`${API}/chat`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({workspace_id: workspaceId, message: userMsg, history: chat, skip_clarify: skipClarify}),
      });
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let assistantMsg = "";
      let sseEventType = "";
      let gotAnyToken = false;

      const flushAssistant = () => {
        if (!gotAnyToken && !assistantMsg) return;
        const finalText = assistantMsg;
        setChat(prev => {
          const copy = [...prev];
          // если последнее сообщение ассистента — дописываем, иначе добавляем
          if (copy.length && copy[copy.length - 1].role === "assistant" && copy[copy.length - 1]._live) {
            copy[copy.length - 1] = {...copy[copy.length - 1], content: finalText, _live: false};
          } else {
            copy.push({role: "assistant", content: finalText, _live: false});
          }
          return copy;
        });
        gotAnyToken = false;
        assistantMsg = "";
      };

      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        const lines = buf.split("\n");
        buf = lines.pop();
        for (const line of lines) {
          if (!line) continue;
          if (line.startsWith("event:")) {
            sseEventType = line.slice(6).trim();
          } else if (line.startsWith("data:")) {
            const raw = line.slice(5).trim();
            let payload = null;
            try { payload = JSON.parse(raw); } catch { payload = raw; }

            switch (sseEventType) {
              case "token": {
                const piece = typeof payload === "string" ? payload : (payload && payload.text) || "";
                assistantMsg += piece;
                gotAnyToken = true;
                setChat(prev => {
                  const copy = [...prev];
                  if (copy.length && copy[copy.length - 1].role === "assistant" && copy[copy.length - 1]._live) {
                    copy[copy.length - 1] = {...copy[copy.length - 1], content: assistantMsg};
                  } else {
                    copy.push({role: "assistant", content: assistantMsg, _live: true});
                  }
                  return copy;
                });
                break;
              }
              case "phase": {
                const p = (payload && payload.phase) || payload;
                if (typeof p === "string") setPhase(p);
                break;
              }
              case "question": {
                // payload: {questions:[...]} | один объект вопроса | массив вопросов
                if (payload && Array.isArray(payload.questions)) {
                  gotQuestions = true;
                  setPendingQuestions(payload.questions);
                  setAnswersByQ({});
                } else if (payload && typeof payload === "object" && payload.question) {
                  gotQuestions = true;
                  setPendingQuestions(prev => {
                    const arr = prev || [];
                    if (arr.some(q => q.id === payload.id)) return arr;
                    return [...arr, payload];
                  });
                } else if (Array.isArray(payload)) {
                  gotQuestions = true;
                  setPendingQuestions(payload);
                  setAnswersByQ({});
                }
                break;
              }
              case "subtask": {
                const title = (payload && payload.title) || "";
                const status = (payload && payload.status) || "running";
                if (!title) break;
                setSubtasks(prev => {
                  const idx = prev.findIndex(s => s.title === title);
                  if (idx >= 0) {
                    const copy = [...prev];
                    copy[idx] = {...copy[idx], status};
                    return copy;
                  }
                  return [...prev, {title, status}];
                });
                break;
              }
              case "records": {
                const recs = (payload && payload.records) || [];
                setRecords(recs);
                break;
              }
              case "tool_call": {
                const name = (payload && payload.name) || "tool";
                setToolEvents(prev => [...prev, {kind: "call", name, ts: Date.now()}]);
                break;
              }
              case "tool_result": {
                const name = (payload && payload.name) || "tool";
                setToolEvents(prev => [...prev, {kind: "result", name, ts: Date.now()}]);
                break;
              }
              case "answer":
              case "done": {
                // финализация — закрываем "живое" сообщение ассистента
                flushAssistant();
                if (sseEventType === "done") {
                  setPhase("done");
                }
                break;
              }
              default:
                // неизвестный тип — игнорируем
                break;
            }
          }
        }
      }
      flushAssistant();
      if (!gotQuestions) {
        // Заглушку показываем ТОЛЬКО если ассистент так и не добавил ни одного
        // сообщения за этот ход (реально пустой ответ). Флаги gotAnyToken/
        // assistantMsg здесь уже СБРОШЕНЫ внутри flushAssistant(), поэтому
        // опираемся на фактическое состояние чата, иначе «(пустой ответ)»
        // лепится после каждого нормального ответа.
        setChat(prev => {
          const last = prev[prev.length - 1];
          if (!last || last.role !== "assistant") {
            return [...prev, {role: "assistant", content: "(пустой ответ)"}];
          }
          return prev;
        });
      }
    } catch (e) {
      setChat(prev => [...prev, {role: "assistant", content: "Ошибка: " + String(e)}]);
    } finally {
      setChatLoading(false);
      // Подтягиваем в таблицу лазейки, которые агент сохранил за этот ход
      // (audit_save_loophole пишет в loophole_record во время стрима).
      loadRecords();
    }
  }, [chatInput, workspaceId, chat, loadRecords]);

  // ── Уточняющие вопросы: helpers ──────────────────────────────────────────
  const toggleAnswer = (qid, value, multi) => {
    setAnswersByQ(prev => {
      const cur = prev[qid] || {selected: [], other: ""};
      const sel = cur.selected;
      if (multi) {
        const has = sel.includes(value);
        return {...prev, [qid]: {...cur, selected: has ? sel.filter(x => x !== value) : [...sel, value]}};
      }
      return {...prev, [qid]: {...cur, selected: [value]}};
    });
  };

  const setOtherText = (qid, text) => {
    setAnswersByQ(prev => ({...prev, [qid]: {...(prev[qid] || {selected: [], other: ""}), other: text}}));
  };

  const submitAnswers = async () => {
    if (!pendingQuestions || !pendingQuestions.length) return;
    const q = pendingQuestions[0];
    const ans = answersByQ[q.id] || {selected: [], other: ""};
    const answersPayload = pendingQuestions.map(pq => {
      const a = answersByQ[pq.id] || {selected: [], other: ""};
      return {
        question: pq.question,
        selected: a.selected,
        other: a.other,
      };
    });
    try {
      const r = await fetch(`${API}/clarify/answer`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        // ИСХОДНЫЙ запрос пользователя (pendingQuery), НЕ текст уточняющего
        // вопроса — иначе enriched строится из вопроса и агент ищет ерунду
        body: JSON.stringify({question: pendingQuery || q.question, answers: answersPayload}),
      });
      const d = await r.json();
      const enriched = (d && d.enriched_question) || (typeof d === "string" ? d : "");
      setPendingQuestions(null);
      setAnswersByQ({});
      if (enriched) {
        // clarify уже пройден → просим бэкенд пропустить гейт (не зацикливаться)
        // отправляем обогащённый вопрос как новое сообщение в чат
        await sendChat(enriched, {skipClarify: true});
      }
    } catch (e) {
      setChat(prev => [...prev, {role: "assistant", content: "Ошибка отправки ответа: " + String(e)}]);
    }
  };

  // Автоскролл чата вниз.
  useEffect(() => {
    if (chatScrollRef.current) {
      chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight;
    }
  }, [chat, chatLoading, pendingQuestions, subtasks, toolEvents]);

  const fmtDate = (v) => v ? new Date(v).toLocaleDateString("ru-RU") : "—";
  const fmtNum = (v) => v != null ? Number(v).toFixed(2) : "—";

  const verdictLabel = (r) => {
    if (r.is_loophole === true) return "лазейка";
    if (r.is_loophole === false) return "нет";
    return "—";
  };

  const sortArrow = (key) => sortKey === key ? (sortDir === "asc" ? " ▲" : " ▼") : "";

  // Фаза: индекс в PHASES для подсветки. await_clarify показываем на шаге clarify.
  const phaseIdx = phase === "await_clarify" ? 0 : (phase ? PHASES.indexOf(phase) : -1);

  return (
    <div className="lp-layout">
      {/* ── Основная область: фильтры + таблица ─────────────────────────────── */}
      <main className="lp-main">
        <header className="lp-main-header">
          <h1>Лазейки и уязвимости в продуктах банка</h1>
          <div className="lp-header-actions">
            <span className="lp-count-badge">
              {loading ? "…" : sortedRecords.length} записей
            </span>
            <button className="lp-btn" onClick={() => setParsersOpen(true)}
                    disabled={!workspaceId} title="Управление парсерами">
              ⚙ Парсеры
            </button>
            <button className="lp-btn lp-btn-primary" onClick={exportCSV}
                    disabled={loading || sortedRecords.length === 0}>
              ⬇ CSV
            </button>
            <button className="lp-btn" onClick={loadRecords} disabled={loading}>
              {loading ? "…" : "↻ Обновить"}
            </button>
          </div>
        </header>

        {/* Фильтры */}
        <div className="lp-filters">
          <div className="lp-filter">
            <label>Поиск по тексту</label>
            <input type="text" value={fText} onChange={e => setFText(e.target.value)}
                   placeholder="название, фрагмент, ключевое слово…"/>
          </div>
          <div className="lp-filter">
            <label>Банки</label>
            <div className="lp-bank-chips">
              {bankOptions.length === 0 && <span className="lp-muted">—</span>}
              {bankOptions.map(b => (
                <label key={b} className={"lp-chip " + (fBanks.includes(b) ? "lp-chip-on" : "")}>
                  <input type="checkbox" checked={fBanks.includes(b)}
                         onChange={() => {
                           setFBanks(prev => prev.includes(b)
                             ? prev.filter(x => x !== b)
                             : [...prev, b]);
                         }}/>
                  {b}
                </label>
              ))}
            </div>
          </div>
          <div className="lp-filter">
            <label>Период сбора</label>
            <div className="lp-period">
              <input type="date" value={fFrom} onChange={e => setFFrom(e.target.value)}/>
              <span>—</span>
              <input type="date" value={fTo} onChange={e => setFTo(e.target.value)}/>
            </div>
          </div>
          <div className="lp-filter">
            <label>Вердикт</label>
            <select value={fVerdict} onChange={e => setFVerdict(e.target.value)}>
              <option value="all">все</option>
              <option value="loophole">лазейка</option>
              <option value="not">не лазейка</option>
            </select>
          </div>
          <div className="lp-filter">
            <label>Статус</label>
            <select value={fStatus} onChange={e => setFStatus(e.target.value)}>
              <option value="">любой</option>
              <option value="new">new</option>
              <option value="classified">classified</option>
              <option value="exported">exported</option>
            </select>
          </div>
          <div className="lp-filter lp-filter-reset">
            <button className="lp-btn" onClick={() => {
              setFText(""); setFBanks([]); setFFrom(""); setFTo("");
              setFVerdict("all"); setFStatus("");
            }}>Сбросить</button>
          </div>
        </div>

        {/* Таблица */}
        <div className="lp-table-wrap">
          {sortedRecords.length === 0 && !loading ? (
            <div className="lp-empty-state">
              Нет записей по выбранным фильтрам.
            </div>
          ) : (
            <table className="lp-table">
              <thead>
                <tr>
                  <th className="lp-col-check">
                    <input type="checkbox"
                           checked={selected.size === sortedRecords.length && sortedRecords.length > 0}
                           onChange={toggleAll}/>
                  </th>
                  <th className="lp-col-sort" onClick={() => toggleSort("title")}>
                    Запись{sortArrow("title")}
                  </th>
                  <th onClick={() => toggleSort("bank_slug")}>
                    Банк{sortArrow("bank_slug")}
                  </th>
                  <th onClick={() => toggleSort("verdict_confidence")}>
                    Доверие{sortArrow("verdict_confidence")}
                  </th>
                  <th onClick={() => toggleSort("trust_score")}>
                    Trust{sortArrow("trust_score")}
                  </th>
                  <th onClick={() => toggleSort("is_loophole")}>
                    Вердикт{sortArrow("is_loophole")}
                  </th>
                  <th onClick={() => toggleSort("status")}>
                    Статус{sortArrow("status")}
                  </th>
                  <th onClick={() => toggleSort("collected_at")}>
                    Собрано{sortArrow("collected_at")}
                  </th>
                  <th>URL</th>
                </tr>
              </thead>
              <tbody>
                {sortedRecords.map(r => (
                  <tr key={r.record_id}
                      className={selected.has(r.record_id) ? "lp-row-sel" : ""}
                      onClick={() => toggleRow(r.record_id)}>
                    <td className="lp-col-check" onClick={e => e.stopPropagation()}>
                      <input type="checkbox" checked={selected.has(r.record_id)}
                             onChange={() => toggleRow(r.record_id)}/>
                    </td>
                    <td className="lp-cell-title">
                      <div className="lp-title-text">{r.title || r.snippet || "—"}</div>
                      {r.verdict_reason && (
                        <div className="lp-reason" title={r.verdict_reason}>
                          {r.verdict_reason}
                        </div>
                      )}
                    </td>
                    <td>{r.bank_slug || "—"}</td>
                    <td>{fmtNum(r.verdict_confidence)}</td>
                    <td>{fmtNum(r.trust_score)}</td>
                    <td>
                      <span className={"lp-badge " +
                        (r.is_loophole === true ? "lp-badge-bad"
                       : r.is_loophole === false ? "lp-badge-ok" : "lp-badge-na")}>
                        {verdictLabel(r)}
                      </span>
                    </td>
                    <td>
                      <span className="lp-status">{r.status || "—"}</span>
                    </td>
                    <td className="lp-cell-date">{fmtDate(r.collected_at)}</td>
                    <td className="lp-cell-url">
                      {r.url ? <a href={r.url} target="_blank" rel="noopener noreferrer"
                                   onClick={e => e.stopPropagation()}>открыть ↗</a>
                             : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </main>

      {/* ── Правый sidebar: чат ─────────────────────────────────────────────── */}
      <aside className="lp-sidebar">
        <div className="lp-sidebar-header">
          <div className="lp-agent-avatar">AI</div>
          <div style={{flex: 1, minWidth: 0}}>
            <div className="lp-agent-name">Аналитик лазеек</div>
            <div className="lp-agent-status">
              <span className={"lp-dot " + (chatLoading ? "lp-dot-busy" : "lp-dot-online")}></span>
              {chatLoading ? "думает…" : "готов"}
            </div>
          </div>
        </div>

        {/* Индикатор фаз пайплайна */}
        {phase && phase !== "done" && (
          <div className="lp-phase-bar" aria-label="Фазы пайплайна">
            {PHASES.map((p, i) => {
              const cls = "lp-phase-step "
                + (i === phaseIdx ? "lp-phase-active "
                : (i < phaseIdx ? "lp-phase-done " : ""));
              return (
                <div key={p} className={cls.trim()}>
                  <span className="lp-phase-dot">{i < phaseIdx ? "✓" : (i + 1)}</span>
                  <span className="lp-phase-label">{p}</span>
                </div>
              );
            })}
          </div>
        )}
        {phase === "done" && (
          <div className="lp-phase-bar lp-phase-bar-done">
            {PHASES.map((p, i) => (
              <div key={p} className="lp-phase-step lp-phase-done">
                <span className="lp-phase-dot">✓</span>
                <span className="lp-phase-label">{p}</span>
              </div>
            ))}
          </div>
        )}

        <div className="lp-chat-messages" ref={chatScrollRef}>
          {chat.length === 0 && (
            <div className="lp-chat-empty">
              Задайте вопрос аналитику по найденным лазейкам.
              Доступны команды: <code>/web_search</code>, <code>/web_fetch</code>,
              <code>/retrieve</code>, <code>/export</code>.
            </div>
          )}

          {/* Tool-бейджи: маленькие метки tool_call/tool_result */}
          {toolEvents.length > 0 && (
            <div className="lp-tool-events">
              {toolEvents.slice(-8).map((ev, i) => (
                <span key={i}
                      className={"lp-tool-badge lp-tool-" + ev.kind}
                      title={ev.kind === "call" ? "вызов инструмента" : "результат"}>
                  {ev.kind === "call" ? "🔧" : "📦"} {ev.name}
                </span>
              ))}
            </div>
          )}

          {/* Подзадачи */}
          {subtasks.length > 0 && (
            <div className="lp-subtasks">
              <div className="lp-subtasks-title">Подзадачи</div>
              {subtasks.map((s, i) => (
                <div key={i} className="lp-subtask">
                  <span className={"lp-subtask-icon lp-subtask-" + s.status}>
                    {s.status === "done" ? "✅" : s.status === "error" ? "❌" : "⏳"}
                  </span>
                  <span className="lp-subtask-title">{s.title}</span>
                </div>
              ))}
            </div>
          )}

          {chat.map((m, i) => (
            <div key={i} className={"lp-bubble lp-bubble-" + m.role}>
              <div className="lp-bubble-role">
                {m.role === "user" ? "Вы" : "Аналитик"}
              </div>
              <div className="lp-bubble-content">{m.content}</div>
            </div>
          ))}
          {chatLoading && (
            <div className="lp-bubble lp-bubble-assistant lp-typing">
              <div className="lp-bubble-role">Аналитик</div>
              <div className="lp-typing-dots">
                <span></span><span></span><span></span>
              </div>
            </div>
          )}
        </div>

        {/* Карточка уточняющих вопросов — между сообщениями и input-area */}
        {pendingQuestions && pendingQuestions.length > 0 && (() => {
          const q = pendingQuestions[0];
          const a = answersByQ[q.id] || {selected: [], other: ""};
          const multi = q.type === "multi";
          return (
            <div className="lp-questions-card">
              <div className="lp-questions-header">Уточняющий вопрос</div>
              <div className="lp-question">
                <div className="lp-question-text">{q.question}</div>
                <div className="lp-question-options">
                  {(q.options || []).map((opt, i) => {
                    const checked = a.selected.includes(opt.value);
                    return (
                      <label key={i}
                             className={"lp-option " + (checked ? "lp-option-on" : "")}>
                        <input
                          type={multi ? "checkbox" : "radio"}
                          name={"q-" + q.id}
                          checked={checked}
                          onChange={() => toggleAnswer(q.id, opt.value, multi)}
                        />
                        <span className="lp-option-label">
                          {opt.label || opt.value}
                          {opt.recommended ? <span className="lp-option-rec"> рекомендуем</span> : null}
                        </span>
                      </label>
                    );
                  })}
                </div>
                {q.allow_other && (
                  <div className="lp-question-other">
                    <label>Свой вариант</label>
                    <textarea
                      rows={2}
                      value={a.other || ""}
                      onChange={e => setOtherText(q.id, e.target.value)}
                      placeholder="Опишите иначе…"
                    />
                  </div>
                )}
                <div className="lp-question-actions">
                  <button className="lp-btn lp-btn-primary lp-btn-sm"
                          onClick={submitAnswers}>
                    Ответить
                  </button>
                </div>
              </div>
            </div>
          );
        })()}

        <div className="lp-chat-input-area">
          <textarea
            className="lp-chat-input"
            rows={2}
            value={chatInput}
            onChange={e => setChatInput(e.target.value)}
            onKeyDown={e => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (!(pendingQuestions && pendingQuestions.length > 0) && chatInput.trim()) sendChat();
              }
            }}
            placeholder={(pendingQuestions && pendingQuestions.length > 0)
              ? "Сначала ответьте на уточняющий вопрос…"
              : "Сообщение аналитику…"}
            disabled={chatLoading || !workspaceId || (pendingQuestions && pendingQuestions.length > 0)}
          />
          <button
            className="lp-chat-send"
            onClick={() => sendChat()}
            disabled={chatLoading || !workspaceId || !chatInput.trim() || (pendingQuestions && pendingQuestions.length > 0)}
          >
            {chatLoading ? "…" : "➤"}
          </button>
        </div>
      </aside>

      {/* ── Модал парсеров ──────────────────────────────────────────────────── */}
      {parsersOpen && (
        <div className="lp-parsers-modal" onClick={() => setParsersOpen(false)}>
          <div className="lp-parsers-dialog" onClick={e => e.stopPropagation()}>
            <div className="lp-parsers-header">
              <h2>Парсеры</h2>
              <button className="lp-btn" onClick={() => setParsersOpen(false)}>✕</button>
            </div>

            <div className="lp-parsers-create">
              <input
                type="text"
                value={newParserQuery}
                onChange={e => setNewParserQuery(e.target.value)}
                placeholder="Запрос для нового парсера (например: 'ипотечные продукты')"
                onKeyDown={e => { if (e.key === "Enter") createParser(); }}
              />
              <button className="lp-btn lp-btn-primary"
                      onClick={createParser}
                      disabled={parsersBusy || !newParserQuery.trim()}>
                Создать
              </button>
            </div>

            <div className="lp-parsers-list">
              {parsers.length === 0 && (
                <div className="lp-empty-state">Парсеры не созданы.</div>
              )}
              {parsers.map(p => (
                <div key={p.parser_id} className="lp-parser-row">
                  <div className="lp-parser-info">
                    <div className="lp-parser-name">{p.name || p.code_path || p.parser_id}</div>
                    <div className="lp-parser-meta">
                      <code>{p.code_path}</code>
                      {p.pid != null && <span> · pid: {p.pid}</span>}
                      {p.running && <span className="lp-parser-running"> · running</span>}
                    </div>
                  </div>
                  <div className="lp-parser-actions">
                    <button className="lp-btn lp-btn-sm"
                            onClick={() => startParser(p.parser_id)}
                            disabled={parsersBusy || p.running}>
                      ▶ Запустить
                    </button>
                    <button className="lp-btn lp-btn-sm"
                            onClick={() => stopParser(p.parser_id)}
                            disabled={parsersBusy || !p.running}>
                      ■ Остановить
                    </button>
                    <button className="lp-btn lp-btn-sm"
                            onClick={async () => {
                              const s = await statusParser(p.parser_id);
                              if (s) alert(JSON.stringify(s, null, 2));
                            }}>
                      Статус
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const root = ReactDOM.createRoot(document.getElementById("loophole-root"));
root.render(<LoopholeApp />);