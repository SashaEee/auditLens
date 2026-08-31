/* loophole.jsx — модуль loophole: левый sidebar-чат (AI-agent стиль) +
   основная область с таблицей найденных лазеек из БД, фильтрами и CSV-экспортом. */
const { useState, useEffect, useRef, useCallback, useMemo } = React;

const API = "/api/loophole";

// Максимум записей в одной CSV-выгрузке (дублирует EXPORT_LIMIT на бэкенде).
const EXPORT_LIMIT = 10000;

// Фазы, которые реально сообщает nanobot-пайплайн, включая финальное done.
// Пользователь видит только русские подписи, протокольные ключи не меняются.
const PHASES = ["clarify", "execute", "answer", "done"];

const PHASE_LABELS = {
  clarify: "Уточнение",
  await_clarify: "Ожидает уточнения",
  execute: "Выполнение",
  answer: "Ответ",
  done: "Готово",
  error: "Ошибка",
};

function parserTargetHref(target) {
  const value = String(target || "").trim();
  if (!value) return null;
  if (/^https?:\/\//i.test(value)) {
    try {
      const parsed = new URL(value);
      return parsed.protocol === "http:" || parsed.protocol === "https:" ? value : null;
    } catch {
      return null;
    }
  }
  if (/^@[A-Za-z][A-Za-z0-9_]{4,31}$/.test(value)) {
    return `https://t.me/${value.slice(1)}`;
  }
  if (/^t\.me\/\S+$/i.test(value)) {
    return `https://${value}`;
  }
  return null;
}

function publicChatErrorMessage(value) {
  const message = String(value || "").trim();
  if (
    !message
    || /error calling llm|connection error|apiconnectionerror|connecterror/i.test(message)
    || /http 5\d\d|failed to fetch|networkerror/i.test(message)
  ) {
    return "Аналитик временно недоступен. Повторите запрос через несколько секунд.";
  }
  return message;
}

function SafeMarkdown({content}) {
  const lines = String(content || "").split(/\r?\n/);
  const blocks = [];
  let list = [];
  const flushList = () => { if (list.length) { blocks.push(<ul key={`list-${blocks.length}`}>{list.map((item, i) => <li key={i}>{item}</li>)}</ul>); list = []; } };
  lines.forEach((raw) => {
    const line = raw.trim();
    if (!line) { flushList(); return; }
    const heading = /^(#{1,3})\s+(.+)$/.exec(line);
    const bullet = /^[-*]\s+(.+)$/.exec(line);
    if (heading) { flushList(); const Tag = `h${heading[1].length + 2}`; blocks.push(<Tag key={`h-${blocks.length}`}>{heading[2]}</Tag>); }
    else if (bullet) list.push(bullet[1]);
    else { flushList(); blocks.push(<p key={`p-${blocks.length}`}>{raw}</p>); }
  });
  flushList();
  return <div className="lp-safe-markdown">{blocks}</div>;
}

// ── Активный слой: focus-trap / Escape / возврат фокуса (story 1.4) ─────────
// Общий механизм для модалок и off-canvas панели чата. Фокус циклирует внутри
// активного слоя, Escape закрывает его, после закрытия фокус возвращается на
// контрол, открывший слой. Стек гарантирует, что клавиатурные события
// обрабатывает только верхний слой (модалка подтверждения поверх модалки
// парсеров не закрывает обе сразу).
const FOCUSABLE_SEL =
  'a[href], button:not([disabled]), input:not([disabled]), ' +
  'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
const _lpLayerStack = [];

function useFocusLayer(active, containerRef, onClose, initialFocusRef, restoreFallbackRef) {
  const openerRef = useRef(null);
  // onClose храним в ref, обновляемом каждый рендер: эффект ниже живёт с
  // deps [active] и иначе держал бы устаревшее замыкание.
  const onCloseRef = useRef(onClose);
  useEffect(() => { onCloseRef.current = onClose; });
  useEffect(() => {
    if (!active) return undefined;
    const id = {};
    _lpLayerStack.push(id);
    openerRef.current = document.activeElement;
    const node = containerRef.current;
    const initial = node
      && ((initialFocusRef && initialFocusRef.current) || node.querySelector(FOCUSABLE_SEL));
    if (initial) initial.focus();
    const onKey = (e) => {
      if (_lpLayerStack[_lpLayerStack.length - 1] !== id) return; // не верхний слой
      if (e.key === "Escape") {
        e.preventDefault();
        onCloseRef.current();
        return;
      }
      if (e.key !== "Tab" || !node) return;
      const items = [...node.querySelectorAll(FOCUSABLE_SEL)]
        .filter(el => el.getClientRects().length > 0);
      if (!items.length) { e.preventDefault(); return; }
      const first = items[0];
      const last = items[items.length - 1];
      const cur = document.activeElement;
      // Начальный title может иметь tabindex=-1: он внутри слоя, но не в
      // последовательности Tab, поэтому сразу направляем его к краю цикла.
      if (!items.includes(cur)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
        return;
      }
      const atEdge = e.shiftKey
        ? cur === first
        : cur === last;
      if (atEdge) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      const i = _lpLayerStack.indexOf(id);
      if (i >= 0) _lpLayerStack.splice(i, 1);
      const opener = openerRef.current;
      const fallback = restoreFallbackRef && restoreFallbackRef.current;
      const enabled = (el) => el && document.contains(el) && !el.matches(":disabled");
      // Подтверждённое действие может отключить opener до cleanup (например,
      // «Удалить» при сетевом запросе). Тогда возвращаем фокус в родительский
      // слой на стабильный enabled-контрол, а не теряем его на document.body.
      const restoreTarget = enabled(opener) ? opener : (enabled(fallback) ? fallback : null);
      if (restoreTarget) restoreTarget.focus();
    };
  }, [active]); // eslint-disable-line react-hooks/exhaustive-deps
}

function LoopholeApp() {
  // ── Таблица / фильтры ──────────────────────────────────────────────────────
  const [records, setRecords] = useState([]);
  const [loading, setLoading] = useState(false);
  // Ошибка загрузки записей (story 1.4): отдельная поверхность с «Повторить»,
  // чтобы сбой не маскировался под пустой результат.
  const [recordsError, setRecordsError] = useState(null);
  const recordsRequestRef = useRef(0);
  const [bankOptions, setBankOptions] = useState([]);
  // Фильтры
  const [fText, setFText] = useState("");
  const [fBanks, setFBanks] = useState([]);          // выбранные slug
  const [fFrom, setFFrom] = useState("");
  const [fTo, setFTo] = useState("");
  const [fVerification, setFVerification] = useState("all");
  // Сортировка
  const [sortKey, setSortKey] = useState("verdict_confidence");
  const [sortDir, setSortDir] = useState("desc");
  // Выделение строк
  const [selected, setSelected] = useState(new Set());

  // ── Полный контент записей (ленивая подгрузка) ──────────────────────────
  const [expanded, setExpanded] = useState(new Set());      // record_id с развёрнутым контентом
  const [contentCache, setContentCache] = useState({});     // {id: {loading, data, error}}
  const [fullView, setFullView] = useState(new Set());      // record_id в режиме «развернуть полностью»

  // ── Ручная маркировка вердиктов ───────────────────────────────────────────
  const [verdictModal, setVerdictModal] = useState(null); // {record} | null
  const [markComment, setMarkComment] = useState("");
  const [markBusy, setMarkBusy] = useState(false);
  // Единственный toast (story 1.4): {text, kind} — info | success | error.
  const [toast, setToast] = useState(null);
  const toastTimerRef = useRef(null);
  const [lastCsvDownload, setLastCsvDownload] = useState(null); // {url, filename}
  const csvUrlRef = useRef(null);

  // ── Чат ────────────────────────────────────────────────────────────────────
  const [chat, setChat] = useState([]);
  const [chatInput, setChatInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);
  const [workspaceId, setWorkspaceId] = useState(null);
  const chatScrollRef = useRef(null);

  // ── Авторизация и рабочие контексты (story 1.1) ──────────────────────────
  // authz: null = проверяем доступ, false = отказ (401/403),
  // "error" = сетевая ошибка загрузки контекстов, иначе {contexts}.
  const [authz, setAuthz] = useState(null);
  const [contextsRetry, setContextsRetry] = useState(0);  // +1 = повторить /contexts
  const [view, setView] = useState("catalog"); // catalog | sources | ai_research | queue | admin
  // Панель агента живёт только в контексте AI-исследования (story 1.3): на
  // широком iframe закреплена справа, ниже 1100px — off-canvas поверх контента,
  // по умолчанию скрыта (открывается кнопкой «Открыть чат» в заголовке).
  const [chatOpen, setChatOpen] = useState(() => window.innerWidth >= 1100);
  const [isCompactViewport, setIsCompactViewport] = useState(
    () => window.innerWidth < 1100
  );
  const previousCompactViewportRef = useRef(isCompactViewport);
  const [queueRecords, setQueueRecords] = useState([]);
  const [queueSelectedId, setQueueSelectedId] = useState(null);
  const [queueDenied, setQueueDenied] = useState(false);
  const [queueLoading, setQueueLoading] = useState(false);
  const [queueError, setQueueError] = useState(false);
  const queueRequestRef = useRef(0);
  // ── Администрирование (story 1.5): роль ЦК КС, Telegram-цели, сводный аудит ──
  const [adminDenied, setAdminDenied] = useState(false);
  const [adminLoading, setAdminLoading] = useState(false);
  const [adminError, setAdminError] = useState(false);
  const [adminRoles, setAdminRoles] = useState(null); // {roles, active_experts, max_experts}
  const [adminAudit, setAdminAudit] = useState(null); // сводный обезличенный аудит
  const [grantName, setGrantName] = useState("");
  const [adminBusy, setAdminBusy] = useState(false);
  // Отзыв роли — модальное подтверждение вместо системного диалога (story 1.4).
  const [revokeConfirm, setRevokeConfirm] = useState(null); // username | null
  const revokeDialogRef = useRef(null);
  const revokeCancelRef = useRef(null);
  const chatInputRef = useRef(null);
  // Слои с focus-trap (story 1.4): панель чата, модалки, подтверждение удаления.
  const chatPanelRef = useRef(null);
  const chatTitleRef = useRef(null);
  const sourcesTabRef = useRef(null);
  const verdictDialogRef = useRef(null);
  const confirmDialogRef = useRef(null);
  const confirmCancelRef = useRef(null);
  // Модальное подтверждение деструктивного удаления парсера (story 1.4) —
  // вместо системного confirm-диалога.
  const [deleteConfirm, setDeleteConfirm] = useState(null); // parser | null

  // ── Новый пайплайн: фазы / подзадачи / уточняющие вопросы ────────────────
  const [phase, setPhase] = useState(null);                // текущая фаза
  const [subtasks, setSubtasks] = useState([]);            // [{title, status}]
  const [pendingQuestions, setPendingQuestions] = useState(null); // null | array
  const [pendingQuery, setPendingQuery] = useState("");           // исходный запрос, вызвавший clarify
  const [clarificationToken, setClarificationToken] = useState(null); // одноразовый token сервера
  const [answersByQ, setAnswersByQ] = useState({});        // {qid: {selected:[], other:""}}
  const [clarifySubmitting, setClarifySubmitting] = useState(false); // идёт /clarify/answer
  const [clarifyError, setClarifyError] = useState("");    // inline-ошибка с восстановлением ответа
  const [toolEvents, setToolEvents] = useState([]);        // badges tool_call/tool_result

  // ── Парсеры ───────────────────────────────────────────────────────────────
  const [parsers, setParsers] = useState([]);
  const parsersRequestRef = useRef(0);
  const [parsersLoading, setParsersLoading] = useState(false);
  const [parsersError, setParsersError] = useState(null);
  const [newParserUrl, setNewParserUrl] = useState("");
  const [newParserDescription, setNewParserDescription] = useState("");
  const [parsersBusy, setParsersBusy] = useState(false);
  const [parserError, setParserError] = useState("");
  const [editParserId, setEditParserId] = useState(null);     // id открытой формы
  const [editForm, setEditForm] = useState({name: "", cron_expr: "", auto_enabled: false});
  const [editError, setEditError] = useState("");
  const [logPanel, setLogPanel] = useState(null);  // {parserId, runId, lines, done, error}
  const logRef = useRef(null);
  const logEsRef = useRef(null);  // активный EventSource live-лога

  // Закрытие live-лога при размонтировании (EventSource иначе живёт вечно).
  useEffect(() => () => {
    if (logEsRef.current) logEsRef.current.close();
  }, []);

  // Сначала — ТОЛЬКО контексты: никаких запросов данных до авторизации.
  useEffect(() => {
    fetch(`${API}/contexts`)
      .then(r => {
        // Ответ сервера 401/403 — осознанный отказ (deny-экран);
        // сетевая ошибка уходит в catch → «Сервис недоступен».
        if (!r.ok) { setAuthz(false); return null; }
        return r.json();
      })
      .then(d => { if (d) setAuthz({contexts: d.contexts || []}); })
      .catch(() => setAuthz("error"));
  }, [contextsRetry]);

  // Workspace создаётся только ПОСЛЕ успешной авторизации (не раньше).
  useEffect(() => {
    if (!authz || !authz.contexts) return;
    fetch(`${API}/workspace`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: "default"}),
    })
      .then(r => r.json())
      .then(d => setWorkspaceId(d.workspace_id))
      .catch(() => {});
  }, [authz]);

  // Загружаем список банков для фильтра — тоже только после авторизации.
  useEffect(() => {
    if (!authz || !authz.contexts) return;
    fetch(`${API}/banks`).then(r => r.json()).then(d => {
      setBankOptions(d.banks || []);
    }).catch(() => {});
  }, [authz]);

  // Загружаем записи.
  const loadRecords = useCallback(async () => {
    const requestGeneration = ++recordsRequestRef.current;
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (fText.trim()) params.set("q", fText.trim());
      if (fBanks.length) params.set("bank_slugs", fBanks.join(","));
      if (fFrom) params.set("period_from", fFrom);
      if (fTo) params.set("period_to", fTo);
      params.set("verification_status", fVerification);
      const url = `${API}/catalog${params.toString() ? "?" + params.toString() : ""}`;
      const r = await fetch(url);
      if (requestGeneration !== recordsRequestRef.current) return;
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      if (requestGeneration !== recordsRequestRef.current) return;
      setRecords(d.records || []);
      setRecordsError(null);
    } catch (e) {
      if (requestGeneration !== recordsRequestRef.current) return;
      // Ошибка не маскируется под пустой результат: отдельная поверхность
      // с «Повторить», старые данные не подменяют актуальное состояние.
      setRecords([]);
      setRecordsError(String(e));
    } finally {
      if (requestGeneration === recordsRequestRef.current) {
        setLoading(false);
      }
    }
  }, [fText, fBanks, fFrom, fTo, fVerification]);

  useEffect(() => {
    if (!authz || !authz.contexts) return undefined;
    const timer = setTimeout(() => loadRecords(), 350);
    return () => clearTimeout(timer);
  }, [loadRecords, authz, fText]);

  // Сброс выделения и развёрнутых строк при смене фильтров.
  useEffect(() => { setSelected(new Set()); setExpanded(new Set()); }, [fText, fBanks, fFrom, fTo, fVerification]);

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

  // Нативные кнопки заголовков поддерживают Enter/Space; aria-sort остаётся на th.
  const sortableThProps = (key) => ({
    "aria-sort": sortKey === key
      ? (sortDir === "asc" ? "ascending" : "descending")
      : "none",
  });

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

  // Сброс фильтров каталога — действие «Сбросить» (фильтры + пустая выборка).
  const resetFilters = () => {
    setFText(""); setFBanks([]); setFFrom(""); setFTo(""); setFVerification("all");
  };

  // ── CSV-экспорт выделенных записей ─────────────────────────────────────────
  const recordWord = (count) => {
    const mod100 = count % 100;
    const mod10 = count % 10;
    if (mod100 >= 11 && mod100 <= 14) return "записей";
    if (mod10 === 1) return "запись";
    if (mod10 >= 2 && mod10 <= 4) return "записи";
    return "записей";
  };

  const triggerCsvDownload = (download) => {
    if (!download) return;
    const a = document.createElement("a");
    a.href = download.url;
    a.download = download.filename;
    a.click();
  };

  const exportCSV = useCallback(async () => {
    if (selected.size === 0) {
      showToast("Сначала выделите перечень лазеек для выгрузки в CSV.", "info");
      return;
    }
    if (selected.size > EXPORT_LIMIT) {
      showToast(`Выделено ${selected.size} записей. За один раз можно выгрузить не более ${EXPORT_LIMIT}.`, "info");
      return;
    }
    try {
      const r = await fetch(`${API}/export`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({records: [...selected], format: "csv"}),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => null);
        showToast((d && d.detail) || "Ошибка выгрузки CSV.", "error");
        return;
      }
      const blob = new Blob([await r.text()], {type: "text/csv;charset=utf-8"});
      const url = URL.createObjectURL(blob);
      if (csvUrlRef.current) URL.revokeObjectURL(csvUrlRef.current);
      csvUrlRef.current = url;
      const download = {url, filename: "loopholes.csv"};
      setLastCsvDownload(download);
      triggerCsvDownload(download);
      showToast(`CSV сформирован · ${selected.size} ${recordWord(selected.size)}`, "success");
    } catch (e) {
      showToast("Не удалось выгрузить CSV: " + String(e), "error");
    }
  }, [selected]);

  // ── Единственный toast (story 1.4): типы info | success | error ──────────
  const showToast = (text, kind = "info") => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    setToast({text, kind});
    toastTimerRef.current = setTimeout(() => setToast(null), 4000);
  };

  // Таймер toast очищается при размонтировании (нет setState после unmount).
  useEffect(() => () => {
    clearTimeout(toastTimerRef.current);
    if (csvUrlRef.current) URL.revokeObjectURL(csvUrlRef.current);
  }, []);

  // ── Ручная маркировка: POST /records/verdict + toast результата ──────────
  const markVerdict = async (ids, isLoophole, comment) => {
    if (!ids.length || markBusy) return false;
    setMarkBusy(true);
    try {
      const r = await fetch(`${API}/records/verdict`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          record_ids: ids, is_loophole: isLoophole, comment: comment || null,
        }),
      });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        showToast((d && typeof d.detail === "string" && d.detail) || "Ошибка маркировки.", "error");
        return false;
      }
      if (d && d.skipped && d.skipped.length) {
        showToast(`Пропущено записей: ${d.skipped.length} (не найдены).`, "info");
      } else {
        showToast("Вердикт сохранён.", "success");
      }
      await loadRecords();
      return true;
    } catch (e) {
      showToast("Ошибка маркировки: " + String(e), "error");
      return false;
    } finally {
      setMarkBusy(false);
    }
  };

  // ── Рабочие контексты: переходы и очередь верификации (fail-closed) ───────
  const loadQueue = useCallback(async () => {
    const requestGeneration = ++queueRequestRef.current;
    setQueueLoading(true);
    try {
      const r = await fetch(`${API}/queue`);
      if (requestGeneration !== queueRequestRef.current) return;
      if (r.status === 401 || r.status === 403) {
        // Нет роли или роль отозвана: очищаем ранее загруженные защищённые
        // данные и показываем fail-closed экран без карточек и источников.
        setQueueRecords([]);
        setQueueSelectedId(null);
        setQueueDenied(true);
        setQueueError(false);
        return;
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      if (requestGeneration !== queueRequestRef.current) return;
      setQueueDenied(false);
      setQueueError(false);
      const nextRecords = d.records || [];
      setQueueRecords(nextRecords);
      setQueueSelectedId(prev => nextRecords.some(r => r.record_id === prev)
        ? prev
        : (nextRecords[0] ? nextRecords[0].record_id : null));
    } catch (e) {
      if (requestGeneration !== queueRequestRef.current) return;
      // Сетевая/серверная ошибка — отдельная поверхность с «Повторить»,
      // а не toast: ошибка не должна выглядеть как пустая очередь.
      // queueDenied сбрасываем: после 403 и последующего сбоя сети показываем
      // поверхность ошибки, а не устаревший fail-closed экран.
      setQueueDenied(false);
      setQueueError(true);
      setQueueRecords([]);
      setQueueSelectedId(null);
    } finally {
      if (requestGeneration === queueRequestRef.current) {
        setQueueLoading(false);
      }
    }
  }, []);

  const openContext = (id) => {
    if (id === "queue") {
      // Маршрут переключаем синхронно при клике: поздний ответ /queue
      // не вырывает вид обратно в очередь, если пользователь уже ушёл
      // в другой контекст (race-фикс ревью 1.4).
      setView("queue");
      loadQueue();
      return;
    }
    if (id === "admin") {
      // Административная поверхность — отдельный маршрут (story 1.5):
      // рабочие данные каталога/очереди здесь не загружаются.
      setView("admin");
      loadAdmin();
      return;
    }
    // Маршрут = контекст: каталог и AI-исследование не делят рабочую поверхность.
    setView(id);
  };

  const onContextTabKeyDown = (event) => {
    const tablist = event.currentTarget.closest('[role="tablist"]');
    if (!tablist) return;
    const tabs = [...tablist.querySelectorAll('[role="tab"]')];
    const currentIndex = tabs.indexOf(event.currentTarget);
    if (currentIndex < 0 || tabs.length === 0) return;
    let nextIndex = null;
    if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = tabs.length - 1;
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    } else if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      nextIndex = (currentIndex + 1) % tabs.length;
    }
    if (nextIndex === null) return;
    event.preventDefault();
    const nextTab = tabs[nextIndex];
    nextTab.focus();
    openContext(nextTab.dataset.contextId);
  };

  // ── Администрирование (story 1.5): роль ЦК КС и аудит ───────────────────
  const loadAdmin = useCallback(async () => {
    setAdminLoading(true);
    try {
      const [rRoles, rAudit] = await Promise.all([
        fetch(`${API}/admin/roles`),
        fetch(`${API}/admin/audit`),
      ]);
      if ([rRoles, rAudit].some(r => r.status === 401 || r.status === 403)) {
        // Нет capability module_admin или она отозвана: очищаем ранее
        // загруженные данные и показываем fail-closed экран без деталей.
        setAdminRoles(null);
        setAdminAudit(null);
        setAdminDenied(true);
        setAdminError(false);
        return;
      }
      if (!rRoles.ok || !rAudit.ok) throw new Error("HTTP");
      const [dRoles, dAudit] = await Promise.all([
        rRoles.json(), rAudit.json(),
      ]);
      setAdminDenied(false);
      setAdminError(false);
      setAdminRoles(dRoles);
      setAdminAudit(dAudit.events || []);
    } catch (e) {
      // Сетевая/серверная ошибка — отдельная поверхность с «Повторить».
      setAdminDenied(false);
      setAdminError(true);
    } finally {
      setAdminLoading(false);
    }
  }, []);

  const grantRole = async () => {
    const username = grantName.trim();
    if (!username || adminBusy) return;
    setAdminBusy(true);
    try {
      const r = await fetch(`${API}/admin/roles/grant`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({username}),
      });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        showToast((d && typeof d.detail === "string" && d.detail)
          || "Не удалось назначить роль.", "error");
        return;
      }
      showToast(`Роль эксперта ЦК КС назначена: ${username}.`, "success");
      setGrantName("");
      await loadAdmin();
    } catch (e) {
      showToast("Не удалось назначить роль: " + String(e), "error");
    } finally {
      setAdminBusy(false);
    }
  };

  const revokeRole = async (username) => {
    if (adminBusy) return;
    setAdminBusy(true);
    try {
      const r = await fetch(`${API}/admin/roles/revoke`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({username}),
      });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        showToast((d && typeof d.detail === "string" && d.detail)
          || "Не удалось отозвать роль.", "error");
        return;
      }
      showToast(`Роль эксперта ЦК КС отозвана: ${username}.`, "success");
      setRevokeConfirm(null);
      await loadAdmin();
    } catch (e) {
      showToast("Не удалось отозвать роль: " + String(e), "error");
    } finally {
      setAdminBusy(false);
    }
  };

  // Панель чата рендерится только на маршруте AI-исследования (story 1.3):
  // каталог и очередь верификации не совмещаются с чатом на одной поверхности.
  const chatVisible = view === "ai_research" && chatOpen;
  const chatModalOpen = chatVisible && isCompactViewport;

  useEffect(() => {
    const syncChatViewport = () => {
      const compact = window.innerWidth < 1100;
      const wasCompact = previousCompactViewportRef.current;
      previousCompactViewportRef.current = compact;
      setIsCompactViewport(compact);
      if (!wasCompact && compact) setChatOpen(false);
    };
    window.addEventListener("resize", syncChatViewport);
    return () => window.removeEventListener("resize", syncChatViewport);
  }, []);

  // ── Активные слои: focus-trap, Escape, возврат фокуса (story 1.4) ─────────
  // Панель чата: при открытии фокус — на заголовок панели (дизайн-контракт
  // ADAPTIVE-CHAT-SPEC §4), после закрытия — возврат на кнопку-инициатор.
  // Состояние разговора и черновик при закрытии не сбрасываются (закрытие не
  // отменяет запущенное исследование).
  useFocusLayer(chatModalOpen, chatPanelRef, () => setChatOpen(false), chatTitleRef);
  useFocusLayer(!!verdictModal, verdictDialogRef, () => setVerdictModal(null));
  // Деструктивное действие: начальный фокус — «Отмена», а не «Удалить».
  useFocusLayer(
    !!deleteConfirm, confirmDialogRef, () => setDeleteConfirm(null), confirmCancelRef, sourcesTabRef
  );
  // Отзыв роли ЦК КС (story 1.5): начальный фокус — «Отмена».
  useFocusLayer(!!revokeConfirm, revokeDialogRef, () => setRevokeConfirm(null), revokeCancelRef);

  // ── Парсеры: список + CRUD + polling ───────────────────────────────────────
  const loadParsers = useCallback(async () => {
    const requestGeneration = ++parsersRequestRef.current;
    setParsersLoading(true);
    try {
      const r = await fetch(`${API}/parsers`);
      if (requestGeneration !== parsersRequestRef.current) return;
      const d = await r.json().catch(() => null);
      if (requestGeneration !== parsersRequestRef.current) return;
      if (!r.ok) {
        const detail = d && typeof d.detail === "string" ? d.detail : `HTTP ${r.status}`;
        throw new Error(detail);
      }
      setParsers(d && Array.isArray(d.parsers) ? d.parsers : []);
      setParsersError(null);
    } catch (e) {
      if (requestGeneration !== parsersRequestRef.current) return;
      setParsers([]);
      setParsersError(String(e));
    } finally {
      if (requestGeneration === parsersRequestRef.current) {
        setParsersLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    if (view !== "sources") return;
    loadParsers();
    const t = setInterval(loadParsers, 5000);
    return () => clearInterval(t);
  }, [view, loadParsers]);

  // Автопрокрутка live-лога к последней строке.
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logPanel && logPanel.lines.length]);

  const WEB_TARGET_RE = /^https?:\/\/\S+$/i;
  const createParserRequest = async () => {
    const url = newParserUrl.trim();
    const description = newParserDescription.trim();
    if (!url || !description || !workspaceId) return;
    if (!WEB_TARGET_RE.test(url)) {
      setParserError("Укажите полный URL веб-источника, начиная с http:// или https://");
      return;
    }
    setParsersBusy(true);
    setParserError("");
    try {
      const r = await fetch(`${API}/parser-requests`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({workspace_id: workspaceId, url, description}),
      });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        const det = d && d.detail;
        throw new Error(typeof det === "string" ? det : `Не удалось зарегистрировать заявку (HTTP ${r.status})`);
      }
      setNewParserUrl("");
      setNewParserDescription("");
      showToast(`Заявка №${d.request_id} зарегистрирована`, "success");
      return d;
    } catch (e) {
      const message = e instanceof Error && e.message ? e.message : "Сеть недоступна, заявка не зарегистрирована";
      setParserError(message);
      showToast(message, "error");
      return null;
    } finally {
      setParsersBusy(false);
    }
  };

  // Закрывает активный EventSource live-лога (если есть).
  const closeLogEs = () => {
    if (logEsRef.current) {
      logEsRef.current.close();
      logEsRef.current = null;
    }
  };

  const openLog = (parserId, runId) => {
    setLogPanel({parserId, runId, lines: [], done: null, error: null});
    closeLogEs();  // закрываем предыдущее соединение, чтобы не плодить утечки
    const es = new EventSource(`${API}/parsers/${parserId}/log/stream?run_id=${runId}`);
    let terminal = false;
    logEsRef.current = es;
    es.addEventListener("log", (e) => {
      setLogPanel(prev => prev && prev.runId === runId
        ? {...prev, lines: [...prev.lines, e.data]} : prev);
    });
    es.addEventListener("done", (e) => {
      terminal = true;
      es.close();
      if (logEsRef.current === es) logEsRef.current = null;
      let payload = null;
      try { payload = JSON.parse(e.data); } catch {}
      setLogPanel(prev => prev && prev.runId === runId
        ? {...prev, done: payload || {status: "завершено"}, error: null} : prev);
      loadParsers();
    });
    es.onerror = () => {
      if (terminal) return;
      terminal = true;
      es.close();
      if (logEsRef.current === es) logEsRef.current = null;
      setLogPanel(prev => prev && prev.runId === runId && !prev.done
        ? {...prev, error: "Соединение с журналом прервано. Повторите запуск или обновите список."}
        : prev);
    };
  };

  const startParser = async (pid) => {
    setParsersBusy(true);
    try {
      const r = await fetch(`${API}/parsers/${pid}/run`, {method: "POST"});
      const d = await r.json().catch(() => null);
      if (!r.ok || !d || !d.run_id) {
        throw new Error(
          d && typeof d.detail === "string" ? d.detail : "Запуск невозможен"
        );
      }
      openLog(pid, d.run_id);
      showToast("Парсер запущен.", "success");
      await loadParsers();
    } catch (e) {
      const message = e instanceof Error && e.message ? e.message : "Сеть недоступна, запуск не выполнен";
      showToast(message, "error");
    } finally {
      setParsersBusy(false);
    }
  };

  const healParser = async (pid) => {
    setParsersBusy(true);
    try {
      const r = await fetch(`${API}/parsers/${pid}/heal`, {method: "POST"});
      const d = await r.json().catch(() => null);
      if (!r.ok || !d || !d.heal_run_id) {
        throw new Error(
          d && typeof d.detail === "string" ? d.detail : "Восстановление недоступно"
        );
      }
      openLog(pid, d.heal_run_id);
      showToast("Запущено восстановление парсера.", "success");
    } catch (e) {
      const message = e instanceof Error && e.message ? e.message : "Сеть недоступна, восстановление не выполнено";
      showToast(message, "error");
    } finally {
      setParsersBusy(false);
    }
  };

  const openEdit = (p) => {
    setEditParserId(p.parser_id);
    setEditForm({
      name: p.name || "",
      cron_expr: p.cron_expr || "",
      auto_enabled: !!p.auto_enabled,
    });
    setEditError("");
  };

  const saveEdit = async () => {
    setParsersBusy(true);
    setEditError("");
    try {
      const r = await fetch(`${API}/parsers/${editParserId}`, {
        method: "PATCH", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          name: editForm.name,
          cron_expr: editForm.cron_expr,   // "" очищает расписание (бэкенд → NULL)
          auto_enabled: editForm.auto_enabled,
        }),
      });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        throw new Error(
          d && typeof d.detail === "string" ? d.detail : `Ошибка сохранения (HTTP ${r.status})`
        );
      }
      setEditParserId(null);
      showToast("Настройки парсера сохранены.", "success");
      await loadParsers();
    } catch (e) {
      const message = e instanceof Error && e.message ? e.message : "Сеть недоступна, настройки не сохранены";
      setEditError(message);
      showToast(message, "error");
    } finally {
      setParsersBusy(false);
    }
  };

  // Удаление — только через модальное подтверждение с последствием (story 1.4);
  // системный confirm-диалог не используется.
  const confirmDeleteParser = async () => {
    const p = deleteConfirm;
    if (!p) return;
    setDeleteConfirm(null);
    setParsersBusy(true);
    try {
      const r = await fetch(`${API}/parsers/${p.parser_id}`, {method: "DELETE"});
      if (!r.ok) {
        const d = await r.json();
        showToast(typeof d.detail === "string" ? d.detail : "Удаление невозможно", "error");
      } else {
        showToast("Парсер удалён.", "success");
        if (logPanel && logPanel.parserId === p.parser_id) {
          closeLogEs();
          setLogPanel(null);
        }
      }
      await loadParsers();
    } finally {
      setParsersBusy(false);
    }
  };

  const stopParser = async (pid) => {
    setParsersBusy(true);
    try {
      const r = await fetch(`${API}/parsers/${pid}/stop`, {method: "POST"});
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        throw new Error(
          d && typeof d.detail === "string" ? d.detail : "Остановка невозможна"
        );
      }
      showToast("Парсер остановлен.", "success");
      await loadParsers();
    } catch (e) {
      const message = e instanceof Error && e.message ? e.message : "Сеть недоступна, остановка не выполнена";
      showToast(message, "error");
    } finally {
      setParsersBusy(false);
    }
  };

  // ── Чат: отправка + полный SSE-парсер ──────────────────────────────────────
  const sendChat = useCallback(async (overrideMessage, opts) => {
    const serverClarificationToken = opts && opts.clarificationToken;
    const skipClarify = !!serverClarificationToken;
    const userMsg = overrideMessage != null ? overrideMessage : chatInput;
    if (!userMsg || !userMsg.trim() || !workspaceId) return false;
    // Token одноразовый: новый challenge принимаем только из server-side SSE.
    setClarificationToken(null);
    // запоминаем ИСХОДНЫЙ запрос (не enriched) — из него build_enriched_question
    // соберёт обогащённый вопрос после ответов на уточнения
    if (!skipClarify) {
      setPendingQuery(userMsg);
      setPhase(null);
      setSubtasks([]);
      setChat(prev => [...prev, {role: "user", content: userMsg}]);
    }
    if (overrideMessage == null) setChatInput("");
    setChatLoading(true);
    setClarifyError("");
    setToolEvents([]);
    setPendingQuestions(null);
    let gotQuestions = false;
    let terminalError = false;
    let terminalErrorMessage = "";
    const acceptQuestions = (questions, token) => {
      const normalized = Array.isArray(questions) ? questions.filter(Boolean) : [];
      if (!normalized.length) return;
      gotQuestions = true;
      setPendingQuestions(normalized);
      setAnswersByQ({});
      setClarificationToken(token || null);
      const textQuestions = normalized.filter(q => q && q.type === "text" && q.question);
      if (textQuestions.length) {
        setChat(prev => {
          const copy = [...prev];
          textQuestions.forEach(q => {
            const marker = `${token || "no-token"}:${q.id || q.question}`;
            if (!copy.some(m => m._clarificationQuestion === marker)) {
              copy.push({
                role: "assistant",
                content: q.question,
                _clarificationQuestion: marker,
              });
            }
          });
          return copy;
        });
      }
    };
    try {
      const resp = await fetch(`${API}/chat`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          workspace_id: workspaceId,
          message: userMsg,
          history: chat,
          clarify_token: serverClarificationToken || null,
        }),
      });
      if (!resp.ok || !resp.body) {
        throw new Error(!resp.ok ? `HTTP ${resp.status}` : "Пустой ответ сервера");
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let assistantMsg = "";
      let sseEventType = "";
      let gotAnyToken = false;
      // Идентификатор приходит только в server-side SSE и относится к
      // immutable evidence snapshot, а не к данным, собранным браузером.
      let reportId = null;

      const flushAssistant = () => {
        if (!gotAnyToken && !assistantMsg) return;
        const finalText = assistantMsg;
        setChat(prev => {
          const copy = [...prev];
          // если последнее сообщение ассистента — дописываем, иначе добавляем
          if (copy.length && copy[copy.length - 1].role === "assistant" && copy[copy.length - 1]._live) {
            copy[copy.length - 1] = {
              ...copy[copy.length - 1],
              content: finalText,
              _live: false,
              ...(reportId ? {report_id: reportId} : {}),
            };
          } else {
            copy.push({
              role: "assistant",
              content: finalText,
              _live: false,
              ...(reportId ? {report_id: reportId} : {}),
            });
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
              case "partial": {
                const message = payload && payload.message;
                if (typeof message !== "string" || !message) break;
                assistantMsg += (assistantMsg ? "\n\n" : "") + message;
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
                if (typeof p === "string") {
                  setPhase(p);
                  if (p === "error") {
                    terminalError = true;
                    terminalErrorMessage = publicChatErrorMessage(
                      payload && typeof payload.message === "string"
                        ? payload.message
                        : "Исследование не запустилось."
                    );
                  }
                }
                break;
              }
              case "question": {
                // payload: {questions:[...]} | один объект вопроса | массив вопросов
                if (payload && Array.isArray(payload.questions)) {
                  acceptQuestions(payload.questions, payload.clarification_token);
                } else if (payload && typeof payload === "object" && payload.question) {
                  acceptQuestions([payload], payload.clarification_token);
                } else if (Array.isArray(payload)) {
                  acceptQuestions(payload, null);
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
                if (sseEventType === "done" && !terminalError) {
                  setPhase("done");
                }
                break;
              }
              case "report": {
                if (payload && Number.isInteger(payload.report_id) && payload.report_id > 0) {
                  reportId = payload.report_id;
                  setChat(prev => {
                    const copy = [...prev];
                    for (let index = copy.length - 1; index >= 0; index -= 1) {
                      if (copy[index].role === "assistant" && !copy[index]._clarificationQuestion) {
                        copy[index] = {...copy[index], report_id: reportId};
                        break;
                      }
                    }
                    return copy;
                  });
                  flushAssistant();
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
      if (terminalError) {
        if (!skipClarify) setChatInput(userMsg);
        setChat(prev => [...prev, {
          role: "assistant",
          content: `Ошибка: ${terminalErrorMessage}`,
        }]);
        return false;
      }
      if (!gotQuestions) {
        setPhase("done");
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
      return true;
    } catch (e) {
      const message = publicChatErrorMessage(
        e instanceof Error && e.message ? e.message : String(e)
      );
      setPhase("error");
      if (!skipClarify) setChatInput(userMsg);
      setChat(prev => [...prev, {role: "assistant", content: "Ошибка: " + message}]);
      return false;
    } finally {
      setChatLoading(false);
      // Подтягиваем в таблицу подтверждённые находки, сохранённые серверным
      // этапом после завершения managed-запуска.
      loadRecords();
    }
  }, [chatInput, workspaceId, chat, loadRecords]);

  // ── Уточняющие вопросы: helpers ──────────────────────────────────────────
  const toggleAnswer = (qid, value, multi) => {
    setClarifyError("");
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
    setClarifyError("");
    setAnswersByQ(prev => ({...prev, [qid]: {...(prev[qid] || {selected: [], other: ""}), other: text}}));
  };

  const submitAnswers = async () => {
    if (
      !pendingQuestions
      || !pendingQuestions.length
      || !clarificationToken
      || clarifySubmitting
    ) return;
    const q = pendingQuestions[0];
    const questionsForRetry = pendingQuestions;
    const clarificationTokenForRetry = clarificationToken;
    const answersForRetry = answersByQ;
    const inputForRetry = chatInput;
    const answersPayload = pendingQuestions.map(pq => {
      const a = pq.type === "text"
        ? {selected: [], other: chatInput.trim()}
        : (answersByQ[pq.id] || {selected: [], other: ""});
      return {
        question: pq.question,
        selected: (a.selected || []).filter(Boolean),
        other: (a.other || "").trim(),
      };
    });
    const missingAnswer = answersPayload.some(a => !a.selected.length && !a.other);
    if (missingAnswer) {
      setClarifyError("Ответьте на уточняющий вопрос перед запуском исследования.");
      return;
    }

    const optimisticContent = answersPayload
      .map(a => [...a.selected, a.other].filter(Boolean).join(", "))
      .filter(Boolean)
      .join("; ");
    const optimisticId = `clarify-answer-${Date.now()}-${Math.random()}`;
    setClarifySubmitting(true);
    setClarifyError("");
    setChat(prev => [...prev, {
      role: "user",
      content: optimisticContent,
      _clarificationAnswer: optimisticId,
    }]);
    setChatInput("");
    // Optimistic-state: ответ виден сразу, controls скрыты, busy остаётся
    // непрерывным до окончания следующего /chat с execution token.
    setPendingQuestions(null);
    setClarificationToken(null);
    setAnswersByQ({});
    try {
      const r = await fetch(`${API}/clarify/answer`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        // ИСХОДНЫЙ запрос пользователя (pendingQuery), НЕ текст уточняющего
        // вопроса — иначе enriched строится из вопроса и агент ищет ерунду
        body: JSON.stringify({
          workspace_id: workspaceId,
          question: pendingQuery || q.question,
          answers: answersPayload,
          clarification_token: clarificationToken,
        }),
      });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        const detail = d && d.detail;
        const message = typeof detail === "string"
          ? detail
          : (detail && detail.message) || "Не удалось отправить ответ.";
        const requestError = new Error(message);
        requestError.status = r.status;
        throw requestError;
      }
      const enriched = (d && d.enriched_question) || (typeof d === "string" ? d : "");
      const executionToken = d && d.execution_token;
      if (enriched && executionToken) {
        const confirmedContent = d && typeof d.answer_message === "string"
          ? d.answer_message
          : optimisticContent;
        setChat(prev => prev.map(message => (
          message._clarificationAnswer === optimisticId
            ? {...message, content: confirmedContent}
            : message
        )));
        // Только server-side execution token разрешает продолжить после clarify.
        const chatStarted = await sendChat(enriched, {clarificationToken: executionToken});
        if (!chatStarted) {
          setChatInput(enriched);
          setClarifyError(
            "Ответ на уточнение сохранён, но исследование не запустилось. "
            + "Подготовленный запрос оставлен в поле — отправьте его ещё раз."
          );
        }
      } else {
        throw new Error("Не удалось подтвердить уточнение");
      }
    } catch (e) {
      setChat(prev => prev.filter(message => message._clarificationAnswer !== optimisticId));
      if (e && e.status === 400) {
        const originalQuery = (pendingQuery || q.question || "").trim();
        setPendingQuestions(null);
        setClarificationToken(null);
        setAnswersByQ({});
        setChatInput(
          `${originalQuery}\n\nОтвет на уточнение: ${optimisticContent}`.trim()
        );
        setPhase("error");
        setClarifyError(
          "Уточнение истекло или уже использовано. "
          + "Исходный запрос и ответ оставлены в поле — отправьте их заново."
        );
        return;
      }
      setPendingQuestions(questionsForRetry);
      setClarificationToken(clarificationTokenForRetry);
      setAnswersByQ(answersForRetry);
      setChatInput(inputForRetry);
      setClarifyError(e instanceof Error && e.message
        ? e.message
        : "Не удалось отправить ответ.");
    } finally {
      setClarifySubmitting(false);
    }
  };

  // Автоскролл чата вниз.
  useEffect(() => {
    if (chatScrollRef.current) {
      chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight;
    }
  }, [chat, chatLoading, pendingQuestions, subtasks, toolEvents]);

  const fmtDate = (v) => {
    if (!v) return "—";
    const date = new Date(v);
    if (Number.isNaN(date.getTime())) return "—";
    const hasTime = /[T ]\d{2}:\d{2}/.test(String(v));
    return hasTime
      ? date.toLocaleString("ru-RU", {
          day: "2-digit", month: "2-digit", year: "numeric",
          hour: "2-digit", minute: "2-digit",
        })
      : date.toLocaleDateString("ru-RU");
  };
  const fmtNum = (v) => v != null ? Number(v).toFixed(2) : "—";

  const RECORD_STATUS_LABELS = {
    published: "подтверждено",
    verified: "подтверждено",
    pending: "на проверке",
    classified: "классифицировано",
    monitoring: "мониторинг",
    rejected: "отклонено",
    new: "новая",
    preliminary: "предварительно",
  };
  const recordStatusLabel = (status) => status ? (RECORD_STATUS_LABELS[status] || "—") : "—";

  const queueSelected = queueRecords.find(r => r.record_id === queueSelectedId)
    || queueRecords[0]
    || null;
  const lastResearchQuery = [...chat].reverse().find(
    message => message.role === "user" && !message._clarificationAnswer
  );
  const lastResearchAnswer = [...chat].reverse().find(
    message => message.role === "assistant" && !message._clarificationQuestion
  );
  const phasePosition = phase ? PHASES.indexOf(phase) : -1;
  const researchProgress = phase === "done"
    ? 100
    : (phasePosition >= 0 ? Math.round(((phasePosition + 1) / PHASES.length) * 100) : 0);
  const completedSubtasks = subtasks.filter(task => task.status === "done").length;

  const verdictLabel = (r) => {
    if (r.is_loophole === true) return "лазейка";
    if (r.is_loophole === false) return "не лазейка";
    return "не размечено";
  };

  const importResearchSources = async (reportId) => {
    if (!reportId) return;
    try {
      const r = await fetch(`${API}/research/reports/${reportId}/import-sources`, {method: "POST"});
      const d = await r.json().catch(() => null);
      if (!r.ok) throw new Error((d && d.detail) || "Не удалось перенести источники.");
      if (d.imported) {
        showToast(`В общую базу добавлено предварительных записей: ${d.imported}.`, "success");
        await loadRecords();
      } else {
        showToast("Новых пригодных источников для переноса нет.", "info");
      }
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Не удалось перенести источники.", "error");
    }
  };

  // Ленивая загрузка полного контента записи (кэш — без повторных запросов).
  const toggleContent = (id) => {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
    if (contentCache[id]) return;
    setContentCache(prev => ({...prev, [id]: {loading: true, data: null, error: null}}));
    fetch(`${API}/records/${id}/content`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)))
      .then(data => setContentCache(prev => ({...prev, [id]: {loading: false, data, error: null}})))
      .catch(e => setContentCache(prev => ({...prev, [id]: {loading: false, data: null, error: String(e)}})));
  };

  const toggleFullView = (id) => {
    setFullView(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  // Бейдж статуса контента в строке таблицы.
  const contentBadge = (r) => {
    if (r.content_status === "full")
      return <span className="lp-content-badge" title="Полный контент сохранён">📄</span>;
    if (r.content_status === "truncated")
      return <span className="lp-content-badge" title="Контент обрезан по лимиту">✂</span>;
    if (r.content_status === "fetch_failed" || r.content_status === "empty")
      return <span className="lp-content-badge" title="Контент не загружен">⚠</span>;
    return null; // legacy/нет данных
  };

  // Развёрнутый блок контента под строкой.
  const renderRecordContent = (r) => {
    const entry = contentCache[r.record_id];
    const sourceLink = r.url ? (
      <a href={r.url} target="_blank" rel="noopener noreferrer">открыть источник ↗</a>
    ) : null;
    if (!entry || entry.loading) {
      return <div className="lp-content-block lp-content-loading">Загрузка контента… {sourceLink}</div>;
    }
    if (entry.error) {
      return <div className="lp-content-block lp-content-error">Ошибка загрузки: {entry.error} {sourceLink}</div>;
    }
    const d = entry.data || {};
    const sizeKb = d.raw_text_len ? Math.ceil(d.raw_text_len / 1024) : null;
    const failed = d.content_status === "fetch_failed" || d.content_status === "empty";
    const showFull = fullView.has(r.record_id);
    return (
      <div className="lp-content-block" onClick={e => e.stopPropagation()}>
        <div className="lp-content-head">
          {d.content_status === "full" && <span className="lp-content-badge">📄 полный</span>}
          {d.content_status === "truncated" && <span className="lp-content-badge">✂ обрезан{sizeKb ? ` до ${sizeKb} КБ` : ""}</span>}
          {failed && <span className="lp-content-badge">⚠ контент не загружен</span>}
          {sizeKb != null && <span className="lp-content-meta">{sizeKb} КБ</span>}
          {sourceLink}
          {d.fetched_at && <span className="lp-content-meta">загружено {fmtDate(d.fetched_at)}</span>}
        </div>
        <div className={"lp-content-body" + (showFull ? " lp-content-body-full" : "")}>
          {d.raw_text || "—"}
        </div>
        {failed && (
          <div className="lp-content-note">
            Полный контент не удалось загрузить; показан сохранённый фрагмент.
            Контент станет доступен после backfill.
          </div>
        )}
        {!failed && (d.raw_text_len || 0) > 2000 && (
          <button type="button" className="lp-btn lp-btn-sm lp-content-more"
                  onClick={() => toggleFullView(r.record_id)}>
            {showFull ? "Свернуть" : "Развернуть полностью"}
          </button>
        )}
      </div>
    );
  };

  const sortArrow = (key) => sortKey === key ? (sortDir === "asc" ? " ▲" : " ▼") : "";

  // Фаза: индекс в PHASES для подсветки. await_clarify показываем на шаге clarify.
  const phaseIdx = phase === "await_clarify" ? 0 : (phase ? PHASES.indexOf(phase) : -1);
  const currentQuestions = pendingQuestions || [];
  const pendingTextQuestion = currentQuestions.find(q => q && q.type === "text") || null;
  const selectionQuestions = currentQuestions.filter(q => q && q.type !== "text");
  const textClarification = !!pendingTextQuestion && selectionQuestions.length === 0;
  const selectionAnswersComplete = selectionQuestions.length > 0 && selectionQuestions.every(q => {
    const answer = answersByQ[q.id] || {selected: [], other: ""};
    return answer.selected.length > 0 || !!answer.other.trim();
  });
  const agentBusy = clarifySubmitting || chatLoading;

  // ── Авторизация: fail-closed поверхности без защищённых данных ────────────
  if (authz === null) {
    return <div className="lp-empty-state" style={{padding: 48}}>Проверяем доступ…</div>;
  }
  if (authz === false) {
    return (
      <div className="lp-empty-state" style={{padding: 48}}>
        <h1>Нет доступа к модулю «Лазейки»</h1>
        <p>Учётная запись не авторизована. Обратитесь к администратору модуля.</p>
      </div>
    );
  }
  if (authz === "error") {
    return (
      <div className="lp-empty-state" style={{padding: 48}}>
        <h1>Сервис недоступен</h1>
        <p>Не удалось загрузить рабочие контексты. Проверьте соединение и повторите.</p>
        <button className="lp-btn"
                onClick={() => { setAuthz(null); setContextsRetry(n => n + 1); }}>
          Повторить
        </button>
      </div>
    );
  }

  return (
    <div className={"lp-layout" + (chatVisible ? " lp-layout-chat" : "")}>
      {/* ── Основная область: поверхность выбранного рабочего контекста ──────── */}
      <main className="lp-main">
        <header className="lp-main-header">
          <h1>
            {view === "ai_research" ? "Новое AI-исследование"
              : view === "sources" ? "Заявка на разработку парсера"
              : view === "queue" ? "Очередь верификации"
              : view === "admin" ? "Управление доступом"
              : "Лазейки и уязвимости в продуктах банка"}
          </h1>
          <div className="lp-header-actions">
            {view === "ai_research" && (
              <button className="lp-btn" onClick={() => setChatOpen(o => !o)}>
                {chatOpen ? "Скрыть чат" : "Открыть чат"}
              </button>
            )}
            {view === "catalog" && (<>
            <button className={"lp-btn" + (selected.size > 0 ? " lp-btn-primary" : "")}
                    onClick={exportCSV}
                    disabled={loading || sortedRecords.length === 0}
                    title="Выгрузить выделенные записи в CSV (не более 10000)">
              CSV{selected.size > 0 ? ` · ${selected.size} ${recordWord(selected.size)}` : ""}
            </button>
            </>)}
            {view !== "ai_research" && (
            <button className="lp-btn"
                    onClick={view === "queue" ? loadQueue
                      : view === "admin" ? loadAdmin
                      : view === "sources" ? loadParsers : loadRecords}
                    disabled={loading || queueLoading || adminLoading || parsersLoading}>
              {(loading || queueLoading || adminLoading || parsersLoading) ? "…" : "Обновить"}
            </button>
            )}
          </div>
        </header>

        {/* Рабочие контексты, доступные principal (список пришёл с сервера) */}
        <nav className="lp-context-nav" role="tablist" aria-label="Рабочие контексты"
             aria-orientation="horizontal">
          {authz.contexts.map(c => {
            const active = c.id === view;
            return (
              <button key={c.id} type="button" role="tab"
                      id={`lp-tab-${c.id}`} aria-selected={active}
                      aria-controls={`lp-panel-${c.id}`} tabIndex={active ? 0 : -1}
                      data-context-id={c.id}
                      ref={c.id === "sources" ? sourcesTabRef : null}
                      className={"lp-context-tab" + (active ? " lp-context-tab-active" : "")}
                      onKeyDown={onContextTabKeyDown}
                      onClick={() => openContext(c.id)}>
                {c.title}
              </button>
            );
          })}
        </nav>

        {authz.contexts.filter(c => c.id !== view).map(c => (
          <section key={`lp-panel-placeholder-${c.id}`} id={`lp-panel-${c.id}`}
                   role="tabpanel" aria-labelledby={`lp-tab-${c.id}`} hidden />
        ))}

        {view === "catalog" && (
        <section className="lp-context-panel lp-catalog-panel" id="lp-panel-catalog"
                 role="tabpanel" aria-labelledby="lp-tab-catalog">
        {/* Фильтры */}
        <div className="lp-filters">
          <div className="lp-filter">
            <label htmlFor="lp-filter-text">Поиск по тексту</label>
            <input id="lp-filter-text" type="text" value={fText} onChange={e => setFText(e.target.value)}
                   placeholder="название, фрагмент, ключевое слово…"/>
          </div>
          <div className="lp-filter">
            <label>Банки</label>
            <div className="lp-bank-chips">
              {bankOptions.length === 0 && <span className="lp-muted">—</span>}
              {bankOptions.map(b => (
                <label key={b} htmlFor={`lp-bank-${b}`}
                       className={"lp-chip " + (fBanks.includes(b) ? "lp-chip-on" : "")}>
                  <input id={`lp-bank-${b}`} type="checkbox" checked={fBanks.includes(b)}
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
            <label htmlFor="lp-filter-from">Дата публикации — с</label>
            <div className="lp-period">
              <input id="lp-filter-from" type="date" value={fFrom}
                     onChange={e => setFFrom(e.target.value)}/>
              <span>—</span>
              <label className="lp-sr-only" htmlFor="lp-filter-to">Дата публикации — по</label>
              <input id="lp-filter-to" type="date" value={fTo}
                     onChange={e => setFTo(e.target.value)}/>
            </div>
          </div>
          <div className="lp-filter">
            <label htmlFor="lp-filter-verification">Проверка ЦК КС</label>
            <select id="lp-filter-verification" value={fVerification}
                    onChange={e => setFVerification(e.target.value)}>
              <option value="all">Все</option>
              <option value="verified">Верифицировано ЦК</option>
              <option value="pending">Ожидает верификации</option>
            </select>
          </div>
          <div className="lp-filter lp-filter-scope">
            <span className="lp-filter-label">Тип данных</span>
            <span className="lp-scope-indicator"
                  aria-label="Каталог показывает только лазейки">лазейки</span>
          </div>
          <div className="lp-filter lp-filter-scope">
            <span className="lp-filter-label">Состояния базы</span>
            <span className="lp-scope-indicator"
                  aria-label="Каталог показывает подтверждённые и предварительные записи">
              подтверждённые и предварительные
            </span>
          </div>
          <div className="lp-filter lp-filter-reset">
            <button className="lp-btn" onClick={resetFilters}>Сбросить</button>
          </div>
        </div>

        {/* Таблица: три разные поверхности — загрузка, пусто, ошибка (1.4) */}
        <div className="lp-table-wrap">
          {loading ? (
            <div className="lp-empty-state">Загрузка записей…</div>
          ) : recordsError ? (
            <div className="lp-empty-state">
              <p>Не удалось загрузить записи. Проверьте соединение и повторите.</p>
              <button className="lp-btn" onClick={loadRecords}>Повторить</button>
            </div>
          ) : sortedRecords.length === 0 ? (
            <div className="lp-empty-state">
              <p>Нет записей по выбранным фильтрам.</p>
              <button className="lp-btn" onClick={resetFilters}>Сбросить</button>
            </div>
          ) : (
            <table className="lp-table">
              <thead>
                <tr>
                  <th className="lp-col-check">
                    <label className="lp-checkbox-hit" htmlFor="lp-select-all">
                      <span className="lp-sr-only">Выбрать все записи</span>
                      <input id="lp-select-all" type="checkbox"
                             checked={selected.size === sortedRecords.length && sortedRecords.length > 0}
                             onChange={toggleAll}/>
                    </label>
                  </th>
                  <th className="lp-col-sort" {...sortableThProps("title")}>
                    <button type="button" className="lp-sort-button"
                            onClick={() => toggleSort("title")}>
                      Запись{sortArrow("title")}
                    </button>
                  </th>
                  <th className="lp-col-narrow2" {...sortableThProps("bank_slug")}>
                    <button type="button" className="lp-sort-button"
                            onClick={() => toggleSort("bank_slug")}>
                      Банк{sortArrow("bank_slug")}
                    </button>
                  </th>
                  <th className="lp-col-narrow2" {...sortableThProps("verdict_confidence")}>
                    <button type="button" className="lp-sort-button"
                            onClick={() => toggleSort("verdict_confidence")}>
                      Доверие{sortArrow("verdict_confidence")}
                    </button>
                  </th>
                  <th {...sortableThProps("is_loophole")}>
                    <button type="button" className="lp-sort-button"
                            onClick={() => toggleSort("is_loophole")}>
                      Вердикт{sortArrow("is_loophole")}
                    </button>
                  </th>
                  <th className="lp-col-narrow2" {...sortableThProps("status")}>
                    <button type="button" className="lp-sort-button"
                            onClick={() => toggleSort("status")}>
                      Статус{sortArrow("status")}
                    </button>
                  </th>
                  <th {...sortableThProps("published_at")}>
                    <button type="button" className="lp-sort-button"
                            onClick={() => toggleSort("published_at")}>
                      Дата публикации{sortArrow("published_at")}
                    </button>
                  </th>
                  <th {...sortableThProps("collected_at")}>
                    <button type="button" className="lp-sort-button"
                            onClick={() => toggleSort("collected_at")}>
                      Собрано{sortArrow("collected_at")}
                    </button>
                  </th>
                  <th className="lp-col-narrow1">URL</th>
                </tr>
              </thead>
              <tbody>
                {sortedRecords.map(r => (
                  <React.Fragment key={r.record_id}>
                    <tr className={selected.has(r.record_id) ? "lp-row-sel" : ""}>
                      <td className="lp-col-check" onClick={e => e.stopPropagation()}>
                        <label className="lp-checkbox-hit"
                               htmlFor={`lp-select-record-${r.record_id}`}>
                          <span className="lp-sr-only">Выбрать запись</span>
                          <input id={`lp-select-record-${r.record_id}`} type="checkbox"
                                 checked={selected.has(r.record_id)}
                                 onChange={() => toggleRow(r.record_id)}/>
                        </label>
                      </td>
                      <td className="lp-cell-title">
                        <div className="lp-title-text">
                          <button type="button" className="lp-row-details"
                                  aria-expanded={expanded.has(r.record_id)}
                                  aria-controls={expanded.has(r.record_id) ? `lp-record-details-${r.record_id}` : undefined}
                                  onClick={() => toggleContent(r.record_id)}>
                            <span className="lp-row-details-icon" aria-hidden="true">
                              {expanded.has(r.record_id) ? "▾" : "▸"}
                            </span>
                            <span>{r.title || r.snippet || "—"}</span>
                            {contentBadge(r)}
                          </button>
                        </div>
                        {r.verdict_reason && (
                          <div className="lp-reason" title={r.verdict_reason}>
                            {r.verdict_reason}
                          </div>
                        )}
                      </td>
                      <td className="lp-col-narrow2">{r.bank_slug || "—"}</td>
                      <td className="lp-col-narrow2">{fmtNum(r.verdict_confidence)}</td>
                      <td onClick={e => e.stopPropagation()}>
                        <button type="button"
                                className={"lp-verdict-chip " +
                                  (r.is_loophole === true ? "lp-verdict-chip-bad"
                                 : r.is_loophole === false ? "lp-verdict-chip-ok"
                                 : "lp-verdict-chip-na")}
                                title="Изменить вердикт"
                                onClick={() => { setMarkComment(""); setVerdictModal({record: r}); }}>
                          <span className="lp-verdict-dot"></span>
                          {verdictLabel(r)}
                        </button>
                        {r.verdict_model === "manual" && (
                          <span className="lp-manual-mark"
                                title="Вердикт проставлен вручную">ручная</span>
                        )}
                      </td>
                      <td className="lp-col-narrow2">
                        <span className={"lp-status" + (r.status === "preliminary" ? " lp-status-preliminary" : "")}>
                          {recordStatusLabel(r.status)}
                        </span>
                      </td>
                      <td className="lp-cell-date lp-cell-published">{fmtDate(r.published_at)}</td>
                      <td className="lp-cell-date lp-cell-collected">{fmtDate(r.collected_at)}</td>
                      <td className="lp-cell-url lp-col-narrow1">
                        {r.url ? <a href={r.url} target="_blank" rel="noopener noreferrer"
                                     onClick={e => e.stopPropagation()}>открыть ↗</a>
                               : "—"}
                      </td>
                    </tr>
                    {expanded.has(r.record_id) && (
                      <tr className="lp-content-row">
                        <td id={`lp-record-details-${r.record_id}`} colSpan={9}>
                          {renderRecordContent(r)}
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          )}
        </div>
        </section>)}

        {/* ── Заявка на разработку веб-парсера и read-only каталог источников. ── */}
        {view === "sources" && (
          <section className="lp-sources-surface" id="lp-panel-sources"
                   role="tabpanel" aria-labelledby="lp-tab-sources">
            <div className="lp-source-grid">
              <form className="lp-source-card" onSubmit={e => { e.preventDefault(); createParserRequest(); }}>
                <div className="lp-eyebrow">Веб-источник</div>
                <h2>Заявка на разработку парсера</h2>
                <p className="lp-muted">
                  Укажите страницу и требования. После рассмотрения команда разработки создаст парсер отдельно.
                </p>
                <label htmlFor="lp-parser-url">URL веб-источника</label>
                <input id="lp-parser-url" type="url" value={newParserUrl}
                       onChange={e => { setNewParserUrl(e.target.value); setParserError(""); }}
                       placeholder="https://bank.example/tariffs" />
                <label htmlFor="lp-parser-description">Что собирать</label>
                <textarea id="lp-parser-description" rows={4} value={newParserDescription}
                          onChange={e => { setNewParserDescription(e.target.value); setParserError(""); }}
                          placeholder="Тарифы, комиссии и условия обслуживания" />
                {parserError && <div className="lp-parser-error" role="alert">{parserError}</div>}
                <button type="submit" className="lp-btn lp-btn-primary"
                        disabled={parsersBusy || !workspaceId
                          || !newParserUrl.trim() || !newParserDescription.trim()}>
                  {parsersBusy ? "Отправляем…" : "Отправить заявку"}
                </button>
              </form>
            </div>

            <section className="lp-source-list" aria-labelledby="lp-source-list-title">
              <div className="lp-source-list-header">
                <div>
                  <div className="lp-eyebrow">Контроль источников</div>
                  <h2 id="lp-source-list-title">Подключённые веб-парсеры</h2>
                </div>
                <button type="button" className="lp-btn" onClick={loadParsers}
                        disabled={parsersLoading}>Обновить список</button>
              </div>
              {parsersLoading ? (
                <div className="lp-empty-state">Загрузка парсеров…</div>
              ) : parsersError ? (
                <div className="lp-empty-state">
                  <p>Не удалось загрузить парсеры. Проверьте соединение и повторите.</p>
                  <button className="lp-btn" onClick={loadParsers}>Повторить</button>
                </div>
              ) : parsers.length === 0 ? (
                <div className="lp-empty-state">Парсеры не созданы.</div>
              ) : parsers.map(p => {
                const st = p.last_run && p.last_run.status;
                return (
                  <article key={p.parser_id} className="lp-parser-row">
                    <div className="lp-parser-info">
                      <div className="lp-parser-name">
                        {p.name || `Парсер #${p.parser_id}`}
                        {p.is_running && <span className="lp-badge lp-badge-run">выполняется</span>}
                        {!p.is_running && st === "success" && <span className="lp-badge lp-badge-ok">успех</span>}
                        {!p.is_running && st === "error" && <span className="lp-badge lp-badge-err">ошибка</span>}
                        {!p.is_running && st === "empty" && <span className="lp-badge lp-badge-empty">0 результатов</span>}
                        {p.needs_attention && <span className="lp-badge lp-badge-attn">требует вмешательства</span>}
                      </div>
                      {p.targets && p.targets.length > 0 && (
                        <div className="lp-parser-targets">
                          {p.targets.map((target, i) => (
                            parserTargetHref(target) ? (
                              <a key={i} href={parserTargetHref(target)} target="_blank"
                                 rel="noopener noreferrer">{target}</a>
                            ) : (
                              <span key={i} className="lp-parser-target-plain">{target}</span>
                            )
                          ))}
                        </div>
                      )}
                      <div className="lp-parser-meta">
                        Источников в базе: {p.records_count ?? 0}
                        {p.created_by && ` · автор: ${p.created_by}`}
                      </div>
                      {editParserId === p.parser_id && (
                        <div className="lp-parser-edit">
                          <label htmlFor={`lp-parser-name-${p.parser_id}`}>Название
                            <input id={`lp-parser-name-${p.parser_id}`} type="text"
                                   value={editForm.name}
                                   onChange={e => setEditForm({...editForm, name: e.target.value})} />
                          </label>
                          <label htmlFor={`lp-parser-cron-${p.parser_id}`}>Расписание (cron)
                            <input id={`lp-parser-cron-${p.parser_id}`} type="text"
                                   placeholder="0 5 * * *" value={editForm.cron_expr}
                                   disabled={!editForm.auto_enabled}
                                   onChange={e => setEditForm({...editForm, cron_expr: e.target.value})} />
                          </label>
                          <label className="lp-parser-edit-toggle"
                                 htmlFor={`lp-parser-auto-${p.parser_id}`}>
                            <input id={`lp-parser-auto-${p.parser_id}`} type="checkbox"
                                   checked={editForm.auto_enabled}
                                   onChange={e => setEditForm({...editForm, auto_enabled: e.target.checked})} />
                            Автозапуск включён
                          </label>
                          {editError && <div className="lp-parser-error">{editError}</div>}
                          <div className="lp-parser-edit-actions">
                            <button className="lp-btn lp-btn-sm lp-btn-primary"
                                    onClick={saveEdit} disabled={parsersBusy}>Сохранить</button>
                            <button className="lp-btn lp-btn-sm"
                                    onClick={() => setEditParserId(null)}>Отмена</button>
                            <button className="lp-btn lp-btn-sm"
                                    onClick={() => healParser(p.parser_id)} disabled={parsersBusy}>
                              Анализ и восстановление
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                  </article>
                );
              })}
            </section>
          </section>
        )}

        {/* ── AI-исследование: работа идёт в панели чата, общая база и очередь
               на этой поверхности не показываются ─────────────────────────── */}
        {view === "ai_research" && (
          <section className="lp-research-surface" id="lp-panel-ai_research"
                   role="tabpanel" aria-labelledby="lp-tab-ai_research"
                   aria-label="Ход AI-исследования">
            <div className="lp-research-board">
              <section className="lp-research-card" aria-labelledby="lp-research-params-title">
                <div className="lp-eyebrow">Параметры исследования</div>
                <h2 id="lp-research-params-title">Текущий запрос</h2>
                <dl className="lp-research-kv">
                  <div>
                    <dt>Тема</dt>
                    <dd>{lastResearchQuery ? lastResearchQuery.content : "Запрос ещё не задан"}</dd>
                  </div>
                  <div>
                    <dt>Режим</dt>
                    <dd>Поиск лазеек с проверкой первоисточников</dd>
                  </div>
                  <div>
                    <dt>Данные</dt>
                    <dd>{records.length} {recordWord(records.length)} в общей базе</dd>
                  </div>
                </dl>
              </section>

              <section className="lp-research-card" aria-labelledby="lp-research-progress-title">
                <div className="lp-research-card-head">
                  <div>
                    <div className="lp-eyebrow">Прогресс исследования</div>
                    <h2 id="lp-research-progress-title">
                      {phase ? (PHASE_LABELS[phase] || phase) : "Ожидает запуска"}
                    </h2>
                  </div>
                  <strong>{researchProgress}%</strong>
                </div>
                <div className="lp-research-progress" aria-label={`Выполнено ${researchProgress}%`}>
                  <span style={{width: `${researchProgress}%`}}></span>
                </div>
                <div className="lp-research-task-summary">
                  Выполнено подзадач: {completedSubtasks} из {subtasks.length}
                </div>
                {subtasks.length > 0 ? (
                  <ul className="lp-research-task-list">
                    {subtasks.map((task, index) => (
                      <li key={index} className={`lp-research-task-${task.status}`}>
                        <span aria-hidden="true"></span>{task.title}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="lp-muted">Подзадачи появятся после запуска исследования.</p>
                )}
              </section>

              <section className="lp-research-card lp-research-evidence"
                       aria-labelledby="lp-research-evidence-title">
                <div className="lp-eyebrow">Доказательства и источники</div>
                <h2 id="lp-research-evidence-title">Промежуточный результат</h2>
                <div className="lp-research-card-head">
                  <div><SafeMarkdown content={lastResearchAnswer
                    ? lastResearchAnswer.content
                    : "После запуска здесь появится проверенный промежуточный вывод аналитика."} /></div>
                  {lastResearchAnswer && lastResearchAnswer.report_id && (
                    <div className="lp-research-result-actions">
                      <details className="lp-research-downloads">
                        <summary className="lp-btn lp-btn-sm">Скачать исследование</summary>
                        <div className="lp-research-download-options" aria-label="Формат скачивания">
                          <a className="lp-btn lp-btn-sm" href={`${API}/research/reports/${lastResearchAnswer.report_id}/export/pdf`}>PDF</a>
                          <a className="lp-btn lp-btn-sm" href={`${API}/research/reports/${lastResearchAnswer.report_id}/export/docx`}>Word</a>
                        </div>
                      </details>
                      <button type="button" className="lp-btn lp-btn-sm"
                              onClick={() => importResearchSources(lastResearchAnswer.report_id)}>
                        Добавить в общую базу
                      </button>
                    </div>
                  )}
                </div>
                <div className="lp-research-meta">
                  <span>Событий инструментов: {toolEvents.length}</span>
                  <span>Фаза: {phase ? (PHASE_LABELS[phase] || phase) : "не запущено"}</span>
                </div>
              </section>
            </div>
            {!chatOpen && (
              <p className="lp-research-chat-note">
                Панель аналитика скрыта. Откройте её кнопкой в заголовке, чтобы продолжить.
              </p>
            )}
          </section>
        )}

        {/* ── Очередь верификации ЦК КС (fail-closed при 403/отзыве роли) ── */}
        {view === "queue" && (
          <section className="lp-context-panel lp-queue-panel" id="lp-panel-queue"
                   role="tabpanel" aria-labelledby="lp-tab-queue">
          {
          queueDenied ? (
            <div className="lp-empty-state" style={{padding: 48}}>
              <h2>Нет доступа к очереди верификации</h2>
              <p>Роль эксперта ЦК КС не назначена или отозвана.</p>
              <button className="lp-btn" onClick={() => setView("catalog")}>
                Вернуться к общей базе
              </button>
            </div>
          ) : (
            <div className="lp-table-wrap">
              {queueLoading ? (
                <div className="lp-empty-state">Загрузка очереди…</div>
              ) : queueError ? (
                <div className="lp-empty-state">
                  <p>Не удалось загрузить очередь верификации.</p>
                  <button className="lp-btn" onClick={loadQueue}>Повторить</button>
                </div>
              ) : queueRecords.length === 0 ? (
                <div className="lp-empty-state">
                  <p>Очередь верификации пуста.</p>
                  <button className="lp-btn" onClick={loadQueue}>
                    Сбросить
                  </button>
                </div>
              ) : (
                <div className="lp-queue-review">
                  <section className="lp-queue-list" aria-label="Записи на проверку">
                    <div className="lp-queue-list-head">
                      <span>Очередь ({queueRecords.length})</span>
                      <span>по доверию</span>
                    </div>
                    {queueRecords.map((record, index) => {
                      const active = queueSelected && queueSelected.record_id === record.record_id;
                      return (
                        <button key={record.record_id} type="button"
                                className={`lp-queue-card${active ? " lp-queue-card-active" : ""}`}
                                aria-current={active ? "true" : undefined}
                                onClick={() => setQueueSelectedId(record.record_id)}>
                          <span className="lp-queue-index">{index + 1}.</span>
                          <span className="lp-queue-card-copy">
                            <strong>{record.title || record.snippet || "—"}</strong>
                            <small>{record.bank_slug || "—"} · {fmtDate(record.published_at)}</small>
                          </span>
                          <span className="lp-queue-confidence">
                            <small>Доверие</small>{fmtNum(record.verdict_confidence)}
                          </span>
                        </button>
                      );
                    })}
                  </section>

                  {queueSelected && (
                    <article className="lp-queue-detail" aria-live="polite">
                      <div className="lp-eyebrow">Карточка проверки</div>
                      <h2>{queueSelected.title || queueSelected.snippet || "—"}</h2>
                      <div className="lp-queue-detail-grid">
                        <div><span>Банк</span><strong>{queueSelected.bank_slug || "—"}</strong></div>
                        <div><span>Доверие</span><strong>{fmtNum(queueSelected.verdict_confidence)}</strong></div>
                        <div><span>Статус</span><strong>{recordStatusLabel(queueSelected.status)}</strong></div>
                        <div><span>Дата публикации</span><strong>{fmtDate(queueSelected.published_at)}</strong></div>
                        <div><span>Собрано</span><strong>{fmtDate(queueSelected.collected_at)}</strong></div>
                      </div>
                      <section className="lp-queue-reason" aria-labelledby="lp-queue-reason-title">
                        <h3 id="lp-queue-reason-title">Комментарий классификатора</h3>
                        <p>{queueSelected.verdict_reason || "Комментарий не указан."}</p>
                      </section>
                      <div className="lp-queue-detail-actions">
                        {queueSelected.url && (
                          <a className="lp-btn" href={queueSelected.url} target="_blank"
                             rel="noopener noreferrer">Открыть источник</a>
                        )}
                        <button type="button" className="lp-btn lp-btn-primary"
                                onClick={() => { setMarkComment(""); setVerdictModal({record: queueSelected}); }}>
                          Проверить вердикт
                        </button>
                      </div>
                    </article>
                  )}
                </div>
              )}
            </div>
          )}
          </section>
        )}
        {/* ── Администрирование (story 1.5): роль ЦК КС и сводный обезличенный
               аудит. Черновики исследований, очередь,
               каталог и технические payload на этой поверхности не показываются ── */}
        {view === "admin" && (
          <section className="lp-context-panel lp-admin-panel" id="lp-panel-admin"
                   role="tabpanel" aria-labelledby="lp-tab-admin">
          {
          adminDenied ? (
            <div className="lp-empty-state" style={{padding: 48}}>
              <h2>Нет доступа к администрированию</h2>
              <p>Роль администратора модуля не назначена или отозвана.</p>
              <button className="lp-btn" onClick={() => setView("catalog")}>
                Вернуться к общей базе
              </button>
            </div>
          ) : adminLoading && !adminRoles ? (
            <div className="lp-empty-state">Загрузка администрирования…</div>
          ) : adminError ? (
            <div className="lp-empty-state">
              <p>Не удалось загрузить данные администрирования.</p>
              <button className="lp-btn" onClick={loadAdmin}>Повторить</button>
            </div>
          ) : (
            <div className="lp-admin">
              {/* Управление ролью ЦК КС: лимит — не более пяти активных */}
              <section className="lp-admin-section" aria-labelledby="lp-admin-roles-title">
                <h2 id="lp-admin-roles-title">Роль ЦК КС</h2>
                <p className="lp-muted">
                  Активных экспертов: {adminRoles ? adminRoles.active_experts : "…"}
                  {" "}из {adminRoles ? adminRoles.max_experts : 5}
                </p>
                <div className="lp-admin-form">
                  <input type="text" value={grantName}
                         onChange={e => setGrantName(e.target.value)}
                         placeholder="username сотрудника"
                         aria-label="Имя пользователя для назначения роли ЦК КС"/>
                  <button className="lp-btn lp-btn-primary" onClick={grantRole}
                          disabled={adminBusy || !grantName.trim()}>
                    Назначить эксперта ЦК КС
                  </button>
                </div>
                {!adminRoles || adminRoles.roles.length === 0 ? (
                  <div className="lp-empty-state">Назначений роли ЦК КС нет.</div>
                ) : (
                  <div className="lp-table-wrap">
                    <table className="lp-table">
                      <thead>
                        <tr>
                          <th>Пользователь</th>
                          <th>Статус</th>
                          <th className="lp-col-narrow1">Назначено</th>
                          <th className="lp-col-narrow1"></th>
                        </tr>
                      </thead>
                      <tbody>
                        {adminRoles.roles.map(a => (
                          <tr key={a.username}>
                            <td>{a.username}</td>
                            <td>
                              <span className="lp-status">
                                {a.status === "active" ? "активна" : "отозвана"}
                              </span>
                            </td>
                            <td className="lp-cell-date lp-col-narrow1">{fmtDate(a.created_at)}</td>
                            <td className="lp-col-narrow1">
                              {a.status === "active" && (
                                <button className="lp-btn lp-btn-sm"
                                        onClick={() => setRevokeConfirm(a.username)}
                                        disabled={adminBusy}>
                                  Отозвать
                                </button>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>

              {/* Сводный обезличенный аудит: только агрегаты, без username */}
              <section className="lp-admin-section" aria-labelledby="lp-admin-audit-title">
                <h2 id="lp-admin-audit-title">Сводный аудит</h2>
                <p className="lp-muted">
                  Обезличенная сводка событий авторизации и изменений ролей.
                </p>
                {!adminAudit || adminAudit.length === 0 ? (
                  <div className="lp-empty-state">Событий аудита пока нет.</div>
                ) : (
                  <div className="lp-table-wrap">
                    <table className="lp-table">
                      <thead>
                        <tr>
                          <th>Действие</th>
                          <th>Решение</th>
                          <th className="lp-col-narrow2">Событий</th>
                          <th className="lp-col-narrow1">Последнее событие</th>
                        </tr>
                      </thead>
                      <tbody>
                        {adminAudit.map(e => (
                          <tr key={e.action + ":" + e.decision}>
                            <td>{e.action}</td>
                            <td><span className="lp-status">{e.decision}</span></td>
                            <td className="lp-col-narrow2">{e.count}</td>
                            <td className="lp-cell-date lp-col-narrow1">{fmtDate(e.last_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </div>
          )}
          </section>
        )}
      </main>

      {/* ── Панель агента: существует только на маршруте AI-исследования ────── */}
      {chatModalOpen && (
        <button type="button" className="lp-chat-backdrop"
                aria-label="Закрыть чат" tabIndex={-1}
                onClick={() => setChatOpen(false)} />
      )}
      {chatVisible && (<aside ref={chatPanelRef} className="lp-sidebar"
                              role={chatModalOpen ? "dialog" : "complementary"}
                              aria-modal={chatModalOpen ? "true" : undefined}
                              aria-labelledby="lp-chat-title">
        <div className="lp-sidebar-header">
          <div className="lp-agent-avatar">AI</div>
          <div style={{flex: 1, minWidth: 0}}>
            <div ref={chatTitleRef} className="lp-agent-name" id="lp-chat-title" tabIndex={-1}>Аналитик лазеек</div>
            <div className="lp-agent-status">
              <span className={"lp-dot " + (agentBusy ? "lp-dot-busy" : "lp-dot-online")}></span>
              {agentBusy ? "Обдумывает ответ" : "Готов"}
            </div>
          </div>
          <button type="button" className="lp-chat-close"
                  onClick={() => setChatOpen(false)}
                  title="Скрыть чат" aria-label="Скрыть чат">✕</button>
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
                  <span className="lp-phase-label">{PHASE_LABELS[p]}</span>
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
                <span className="lp-phase-label">{PHASE_LABELS[p]}</span>
              </div>
            ))}
          </div>
        )}

        <div className="lp-chat-messages" ref={chatScrollRef}>
          {chat.length === 0 && (
            <div className="lp-chat-empty">
              Задайте вопрос по найденным лазейкам — аналитик уточнит контекст
              и подготовит исследование по доступным источникам.
            </div>
          )}

          {/* Список использованных инструментов без аргументов и результатов */}
          {toolEvents.length > 0 && (
            <div className="lp-tool-events">
              <div className="lp-subtasks-title">Использованные инструменты</div>
              {toolEvents.slice(-8).map((ev, i) => (
                <span key={i}
                      className={"lp-tool-badge lp-tool-" + ev.kind}
                      title={ev.kind === "call" ? "вызов инструмента" : "результат"}>
                  {ev.kind === "call" ? "🔧" : "📦"} {({
                    audit_web_search: "Веб-поиск",
                    audit_web_fetch: "Чтение источника",
                    audit_extract_loopholes: "Извлечение признаков",
                    audit_db_query: "Запрос к базе",
                    audit_table_load: "Загрузка таблицы",
                    audit_export: "Подготовка выгрузки",
                  })[ev.name] || "Инструмент"}
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
          {agentBusy && (
            <div className="lp-bubble lp-bubble-assistant lp-typing">
              <div className="lp-bubble-role">Аналитик</div>
              <div className="lp-typing-dots">
                <span></span><span></span><span></span>
              </div>
            </div>
          )}
        </div>

        {/* Карточка уточняющих вопросов — между сообщениями и input-area */}
        {selectionQuestions.length > 0 && (
          <div className="lp-questions-card">
            <div className="lp-questions-header">Уточняющие вопросы</div>
            {selectionQuestions.map(q => {
              const answer = answersByQ[q.id] || {selected: [], other: ""};
              const multi = q.type === "multi";
              return (
                <div className="lp-question" key={q.id || q.question}>
                  <div className="lp-question-text">{q.question}</div>
                  <div className="lp-question-options">
                    {(q.options || []).map((opt, i) => {
                      const checked = answer.selected.includes(opt.value);
                      const optionInputId = `lp-question-${q.id}-${i}`;
                      return (
                        <label key={opt.value || i} htmlFor={optionInputId}
                               className={"lp-option " + (checked ? "lp-option-on" : "")}>
                          <input
                            id={optionInputId}
                            type={multi ? "checkbox" : "radio"}
                            name={"q-" + q.id}
                            checked={checked}
                            onChange={() => toggleAnswer(q.id, opt.value, multi)}
                          />
                          <span className="lp-option-label">
                            {opt.label || opt.value}
                            {opt.recommended
                              ? <span className="lp-option-rec"> рекомендуем</span>
                              : null}
                          </span>
                        </label>
                      );
                    })}
                  </div>
                  {q.allow_other && (
                    <div className="lp-question-other">
                      <label htmlFor={`lp-question-other-${q.id}`}>Свой вариант</label>
                      <textarea id={`lp-question-other-${q.id}`}
                        rows={2}
                        value={answer.other || ""}
                        onChange={e => setOtherText(q.id, e.target.value)}
                        placeholder="Опишите иначе…"
                      />
                    </div>
                  )}
                </div>
              );
            })}
            {!selectionAnswersComplete && (
              <div className="lp-clarify-hint">Ответьте на все вопросы перед запуском.</div>
            )}
            <div className="lp-question-actions">
              <button className="lp-btn lp-btn-primary lp-btn-sm"
                      disabled={clarifySubmitting || !selectionAnswersComplete}
                      onClick={submitAnswers}>
                {clarifySubmitting ? "Отправляю…" : "Ответить"}
              </button>
            </div>
          </div>
        )}

        {clarifyError && (
          <div className="lp-clarify-error" role="alert">{clarifyError}</div>
        )}

        <div className="lp-chat-input-area">
          <label className="lp-sr-only" htmlFor="lp-chat-input">Сообщение аналитику</label>
          <textarea id="lp-chat-input"
            ref={chatInputRef}
            className="lp-chat-input"
            rows={2}
            value={chatInput}
            onChange={e => {
              setChatInput(e.target.value);
              if (clarifyError) setClarifyError("");
            }}
            onKeyDown={e => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (textClarification && chatInput.trim()) submitAnswers();
                else if (!currentQuestions.length && chatInput.trim()) sendChat();
              }
            }}
            placeholder={textClarification
              ? "Ответ на уточняющий вопрос…"
              : (selectionQuestions.length
                ? "Сначала ответьте на уточняющие вопросы…"
                : "Сообщение аналитику…")}
            disabled={agentBusy || !workspaceId || selectionQuestions.length > 0}
          />
          <button
            className="lp-chat-send"
            type="button"
            aria-label="Отправить сообщение"
            onClick={() => textClarification ? submitAnswers() : sendChat()}
            disabled={agentBusy || !workspaceId || !chatInput.trim() || selectionQuestions.length > 0}
          >
            {agentBusy ? "…" : "➤"}
          </button>
        </div>
      </aside>)}

      {/* ── Модал ручной маркировки вердикта ────────────────────────────────── */}
      {verdictModal && (() => {
        const rec = verdictModal.record;
        const current = rec.is_loophole; // true | false | null
        const choose = async (val) => {
          const ok = await markVerdict([rec.record_id], val, markComment.trim());
          if (ok) setVerdictModal(null);
        };
        return (
          <div className="lp-parsers-modal">
            <button type="button" className="lp-modal-backdrop"
                    aria-label="Закрыть диалог" tabIndex={-1}
                    onClick={() => setVerdictModal(null)} />
            <div className="lp-parsers-dialog lp-verdict-dialog" ref={verdictDialogRef}
                 role="dialog" aria-modal="true" aria-labelledby="lp-verdict-title">
              <div className="lp-parsers-header lp-verdict-header">
                <div>
                  <div className="lp-eyebrow">Ручная маркировка</div>
                  <h2 id="lp-verdict-title">Вердикт записи</h2>
                </div>
                <button className="lp-dialog-x" aria-label="Закрыть"
                        onClick={() => setVerdictModal(null)}>✕</button>
              </div>
              <div className="lp-verdict-body">
                <div className="lp-verdict-record">
                  <div className="lp-verdict-title">
                    {rec.title || rec.snippet || "—"}
                  </div>
                  <div className="lp-verdict-meta">
                    <span>{rec.bank_slug || "банк не указан"}</span>
                    <span>доверие {fmtNum(rec.verdict_confidence)}</span>
                    <span>опубликовано {fmtDate(rec.published_at)}</span>
                    <span>собрано {fmtDate(rec.collected_at)}</span>
                  </div>
                </div>
                <div className="lp-verdict-field">
                  <label htmlFor="lp-mark-comment">Комментарий аудитора</label>
                  <textarea id="lp-mark-comment" rows={2} value={markComment}
                            onChange={e => setMarkComment(e.target.value)}
                            placeholder="Почему это лазейка или обычный запрос…"/>
                </div>
                <div className="lp-verdict-options">
                  {current !== true && (
                    <button className="lp-verdict-option lp-verdict-option-bad"
                            disabled={markBusy} onClick={() => choose(true)}>
                      <span className="lp-verdict-dot"></span>
                      <span className="lp-verdict-option-text">
                        <span className="lp-verdict-option-name">Лазейка</span>
                        <span className="lp-verdict-option-desc">
                          подтверждённая схема обхода условий
                        </span>
                      </span>
                    </button>
                  )}
                  {current !== false && (
                    <button className="lp-verdict-option lp-verdict-option-ok"
                            disabled={markBusy} onClick={() => choose(false)}>
                      <span className="lp-verdict-dot"></span>
                      <span className="lp-verdict-option-text">
                        <span className="lp-verdict-option-name">Обычный запрос</span>
                        <span className="lp-verdict-option-desc">
                          лазейкой не является
                        </span>
                      </span>
                    </button>
                  )}
                </div>
                <div className="lp-verdict-foot">
                  {current != null && (
                    <span className="lp-verdict-current">
                      Текущий вердикт: {verdictLabel(rec)}
                      {rec.verdict_model === "manual" ? " · ручная" : ""}
                    </span>
                  )}
                  <button className="lp-btn lp-btn-sm"
                          onClick={() => setVerdictModal(null)}>
                    Отмена
                  </button>
                </div>
              </div>
            </div>
          </div>
        );
      })()}

      {/* ── Модал подтверждения удаления парсера (деструктивное действие) ──── */}
      {deleteConfirm && (
        <div className="lp-parsers-modal">
          <button type="button" className="lp-modal-backdrop"
                  aria-label="Закрыть диалог" tabIndex={-1}
                  onClick={() => setDeleteConfirm(null)} />
          <div className="lp-parsers-dialog lp-confirm-dialog" ref={confirmDialogRef}
               role="dialog" aria-modal="true" aria-labelledby="lp-confirm-title">
            <div className="lp-parsers-header">
              <h2 id="lp-confirm-title">Удаление парсера</h2>
              <button className="lp-dialog-x" aria-label="Закрыть"
                      onClick={() => setDeleteConfirm(null)}>✕</button>
            </div>
            <div className="lp-confirm-body">
              <p>
                Парсер «{deleteConfirm.name || `Парсер #${deleteConfirm.parser_id}`}»
                будет удалён вместе с кодом и записью. Действие необратимо.
              </p>
              <div className="lp-confirm-actions">
                <button className="lp-btn lp-btn-danger"
                        onClick={confirmDeleteParser}
                        disabled={parsersBusy}>
                  Удалить
                </button>
                <button className="lp-btn" ref={confirmCancelRef}
                        onClick={() => setDeleteConfirm(null)}>
                  Отмена
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── Модал подтверждения отзыва роли ЦК КС (story 1.5) ─────────────── */}
      {revokeConfirm && (
        <div className="lp-parsers-modal">
          <button type="button" className="lp-modal-backdrop"
                  aria-label="Закрыть диалог" tabIndex={-1}
                  onClick={() => setRevokeConfirm(null)} />
          <div className="lp-parsers-dialog lp-confirm-dialog" ref={revokeDialogRef}
               role="dialog" aria-modal="true" aria-labelledby="lp-revoke-title">
            <div className="lp-parsers-header">
              <h2 id="lp-revoke-title">Отзыв роли ЦК КС</h2>
              <button className="lp-dialog-x" aria-label="Закрыть"
                      onClick={() => setRevokeConfirm(null)}>✕</button>
            </div>
            <div className="lp-confirm-body">
              <p>
                У пользователя «{revokeConfirm}» будет отозвана роль эксперта
                ЦК КС: доступ к очереди верификации закроется со следующего
                запроса.
              </p>
              <div className="lp-confirm-actions">
                <button className="lp-btn lp-btn-danger"
                        onClick={() => revokeRole(revokeConfirm)}
                        disabled={adminBusy}>
                  Отозвать
                </button>
                <button className="lp-btn" ref={revokeCancelRef}
                        onClick={() => setRevokeConfirm(null)}>
                  Отмена
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── Единственный toast (info | success | error) ────────────────────── */}
      {toast && (
        <div className={"lp-toast lp-toast-" + toast.kind}
             role={toast.kind === "error" ? "alert" : "status"}>
          <span>{toast.text}</span>
          {toast.kind === "success" && toast.text.startsWith("CSV сформирован")
            && lastCsvDownload && (
            <button type="button" className="lp-toast-action"
                    onClick={() => triggerCsvDownload(lastCsvDownload)}>
              Скачать повторно
            </button>
          )}
        </div>
      )}
    </div>
  );
}

const root = ReactDOM.createRoot(document.getElementById("loophole-root"));
root.render(<LoopholeApp />);
