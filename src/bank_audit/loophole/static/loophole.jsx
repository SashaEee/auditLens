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
  execute: "Выполнение",
  answer: "Ответ",
  done: "Готово",
};

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
  const [fVerdict, setFVerdict] = useState("all");   // all | loophole | not | null
  const [fStatus, setFStatus] = useState("");
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
  const [bulkComment, setBulkComment] = useState("");
  const [markBusy, setMarkBusy] = useState(false);
  // Единственный toast (story 1.4): {text, kind} — info | success | error.
  const [toast, setToast] = useState(null);
  const toastTimerRef = useRef(null);

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
  const [view, setView] = useState("catalog");        // catalog | ai_research | queue
  // Панель агента живёт только в контексте AI-исследования (story 1.3): на
  // широком iframe закреплена справа, ниже 1100px — off-canvas поверх контента,
  // по умолчанию скрыта (открывается кнопкой «Открыть чат» в заголовке).
  const [chatOpen, setChatOpen] = useState(() => window.innerWidth >= 1100);
  const [isCompactViewport, setIsCompactViewport] = useState(
    () => window.innerWidth < 1100
  );
  const previousCompactViewportRef = useRef(isCompactViewport);
  const [queueRecords, setQueueRecords] = useState([]);
  const [queueDenied, setQueueDenied] = useState(false);
  const [queueLoading, setQueueLoading] = useState(false);
  const [queueError, setQueueError] = useState(false);
  const queueRequestRef = useRef(0);
  // ── Администрирование (story 1.5): роль ЦК КС, Telegram-цели, сводный аудит ──
  const [adminDenied, setAdminDenied] = useState(false);
  const [adminLoading, setAdminLoading] = useState(false);
  const [adminError, setAdminError] = useState(false);
  const [adminRoles, setAdminRoles] = useState(null);     // {roles, active_experts, max_experts}
  const [adminTargets, setAdminTargets] = useState(null); // статус Telegram-целей
  const [adminAudit, setAdminAudit] = useState(null);     // сводный обезличенный аудит
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
  const parsersDialogRef = useRef(null);
  const parsersCloseRef = useRef(null);
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
  const [answersByQ, setAnswersByQ] = useState({});        // {qid: {selected:[], other:""}}
  const [clarifySubmitting, setClarifySubmitting] = useState(false); // идёт /clarify/answer
  const [toolEvents, setToolEvents] = useState([]);        // badges tool_call/tool_result

  // ── Парсеры ───────────────────────────────────────────────────────────────
  const [parsersOpen, setParsersOpen] = useState(false);
  const [parsers, setParsers] = useState([]);
  const parsersRequestRef = useRef(0);
  const [parsersLoading, setParsersLoading] = useState(false);
  const [parsersError, setParsersError] = useState(null);
  const [newParserQuery, setNewParserQuery] = useState("");
  const [parsersBusy, setParsersBusy] = useState(false);
  const [parserError, setParserError] = useState("");
  const [editParserId, setEditParserId] = useState(null);     // id открытой формы
  const [editForm, setEditForm] = useState({name: "", cron_expr: "", auto_enabled: false});
  const [editError, setEditError] = useState("");
  const [logPanel, setLogPanel] = useState(null);  // {parserId, runId, lines, done}
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
      if (fVerdict === "loophole") params.set("only_loophole", "true");
      else if (fVerdict === "not") params.set("only_loophole", "false");
      if (fStatus) params.set("status", fStatus);
      const url = `${API}/records${params.toString() ? "?" + params.toString() : ""}`;
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
  }, [fText, fBanks, fFrom, fTo, fVerdict, fStatus]);

  useEffect(() => { if (authz && authz.contexts) loadRecords(); }, [loadRecords, authz]);

  // Сброс выделения и развёрнутых строк при смене фильтров.
  useEffect(() => { setSelected(new Set()); setExpanded(new Set()); }, [fText, fBanks, fFrom, fTo, fVerdict, fStatus]);

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
    setFText(""); setFBanks([]); setFFrom(""); setFTo("");
    setFVerdict("all"); setFStatus("");
  };

  // ── CSV-экспорт выделенных записей ─────────────────────────────────────────
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
      const a = document.createElement("a");
      a.href = url; a.download = "loopholes.csv"; a.click();
      URL.revokeObjectURL(url);
      showToast(`Выгружено записей: ${selected.size}.`, "success");
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
  useEffect(() => () => clearTimeout(toastTimerRef.current), []);

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
        setQueueDenied(true);
        setQueueError(false);
        return;
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      if (requestGeneration !== queueRequestRef.current) return;
      setQueueDenied(false);
      setQueueError(false);
      setQueueRecords(d.records || []);
    } catch (e) {
      if (requestGeneration !== queueRequestRef.current) return;
      // Сетевая/серверная ошибка — отдельная поверхность с «Повторить»,
      // а не toast: ошибка не должна выглядеть как пустая очередь.
      // queueDenied сбрасываем: после 403 и последующего сбоя сети показываем
      // поверхность ошибки, а не устаревший fail-closed экран.
      setQueueDenied(false);
      setQueueError(true);
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

  // ── Администрирование (story 1.5): роль ЦК КС, Telegram-цели, аудит ──────
  const loadAdmin = useCallback(async () => {
    setAdminLoading(true);
    try {
      const [rRoles, rTargets, rAudit] = await Promise.all([
        fetch(`${API}/admin/roles`),
        fetch(`${API}/admin/telegram-targets`),
        fetch(`${API}/admin/audit`),
      ]);
      if ([rRoles, rTargets, rAudit].some(r => r.status === 401 || r.status === 403)) {
        // Нет capability module_admin или она отозвана: очищаем ранее
        // загруженные данные и показываем fail-closed экран без деталей.
        setAdminRoles(null);
        setAdminTargets(null);
        setAdminAudit(null);
        setAdminDenied(true);
        setAdminError(false);
        return;
      }
      if (!rRoles.ok || !rTargets.ok || !rAudit.ok) throw new Error("HTTP");
      const [dRoles, dTargets, dAudit] = await Promise.all([
        rRoles.json(), rTargets.json(), rAudit.json(),
      ]);
      setAdminDenied(false);
      setAdminError(false);
      setAdminRoles(dRoles);
      setAdminTargets(dTargets.targets || []);
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
  useFocusLayer(parsersOpen, parsersDialogRef, () => setParsersOpen(false));
  useFocusLayer(!!verdictModal, verdictDialogRef, () => setVerdictModal(null));
  // Деструктивное действие: начальный фокус — «Отмена», а не «Удалить».
  useFocusLayer(
    !!deleteConfirm, confirmDialogRef, () => setDeleteConfirm(null), confirmCancelRef, parsersCloseRef
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
    if (!parsersOpen) return;
    loadParsers();
    const t = setInterval(loadParsers, 5000);
    return () => clearInterval(t);
  }, [parsersOpen, loadParsers]);

  // Автопрокрутка live-лога к последней строке.
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logPanel && logPanel.lines.length]);

  // URL ресурса или группа мессенджера — обязательны для создания парсера.
  const TARGET_RE = /(?:https?:\/\/)?(?:www\.)?(?:t|telegram)\.me\/\S+|https?:\/\/\S+|@[A-Za-z][A-Za-z0-9_]{4,31}\b/i;
  const hasTarget = (q) => TARGET_RE.test(q || "");

  const createParser = async () => {
    const q = newParserQuery.trim();
    if (!q || !workspaceId) return;
    if (!hasTarget(q)) {
      setParserError("Укажите URL ресурса или группу мессенджера (например: https://example.com или https://t.me/group_name)");
      return;
    }
    setParsersBusy(true);
    setParserError("");
    try {
      const r = await fetch(`${API}/parsers`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({workspace_id: workspaceId, query: q}),
      });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        const det = d && d.detail;
        if (r.status === 409 && det && det.conflict_with) {
          throw new Error(
            `Такой источник уже парсит «${det.conflict_with.name || det.conflict_with.parser_id}» (id ${det.conflict_with.parser_id})`
          );
        } else {
          throw new Error(
            typeof det === "string" ? det : `Ошибка создания парсера (HTTP ${r.status})`
          );
        }
      }
      setNewParserQuery("");
      const warning = d && d.warnings && d.warnings.length
        ? ` Частичное пересечение источников с парсером id ${d.warnings[0].conflict_with}.`
        : "";
      showToast(`Парсер создан.${warning}`, "success");
      await loadParsers();
      return d;
    } catch (e) {
      const message = e instanceof Error && e.message ? e.message : "Сеть недоступна, парсер не создан";
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
    setLogPanel({parserId, runId, lines: [], done: null});
    closeLogEs();  // закрываем предыдущее соединение, чтобы не плодить утечки
    const es = new EventSource(`${API}/parsers/${parserId}/log/stream?run_id=${runId}`);
    logEsRef.current = es;
    es.addEventListener("log", (e) => {
      setLogPanel(prev => prev && prev.runId === runId
        ? {...prev, lines: [...prev.lines, e.data]} : prev);
    });
    es.addEventListener("done", (e) => {
      es.close();
      logEsRef.current = null;
      let payload = null;
      try { payload = JSON.parse(e.data); } catch {}
      setLogPanel(prev => prev && prev.runId === runId ? {...prev, done: payload} : prev);
      loadParsers();
    });
    es.onerror = () => { es.close(); logEsRef.current = null; };
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
    const skipClarify = !!(opts && opts.skipClarify);
    const userMsg = overrideMessage != null ? overrideMessage : chatInput;
    if (!userMsg || !userMsg.trim() || !workspaceId) return;
    // запоминаем ИСХОДНЫЙ запрос (не enriched) — из него build_enriched_question
    // соберёт обогащённый вопрос после ответов на уточнения
    if (!skipClarify) {
      setPendingQuery(userMsg);
      setPhase(null);
      setSubtasks([]);
    }
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
      if (!resp.ok || !resp.body) {
        throw new Error(!resp.ok ? `HTTP ${resp.status}` : "Пустой ответ сервера");
      }
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
    if (!pendingQuestions || !pendingQuestions.length || clarifySubmitting) return;
    setClarifySubmitting(true);
    const q = pendingQuestions[0];
    const answersPayload = pendingQuestions.map(pq => {
      const a = answersByQ[pq.id] || {selected: [], other: ""};
      return {
        question: pq.question,
        selected: a.selected,
        other: a.other,
      };
    });
    // Закрываем окно ДО запроса: /clarify/answer ждёт LLM до ~70с, иначе
    // пользователь видит «зависшую» карточку без какой-либо реакции.
    setPendingQuestions(null);
    setAnswersByQ({});
    try {
      const r = await fetch(`${API}/clarify/answer`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        // ИСХОДНЫЙ запрос пользователя (pendingQuery), НЕ текст уточняющего
        // вопроса — иначе enriched строится из вопроса и агент ищет ерунду
        body: JSON.stringify({question: pendingQuery || q.question, answers: answersPayload}),
      });
      const d = await r.json();
      const enriched = (d && d.enriched_question) || (typeof d === "string" ? d : "");
      if (enriched) {
        // clarify уже пройден → просим бэкенд пропустить гейт (не зацикливаться)
        // отправляем обогащённый вопрос как новое сообщение в чат
        await sendChat(enriched, {skipClarify: true});
      }
    } catch (e) {
      setChat(prev => [...prev, {role: "assistant", content: "Ошибка отправки ответа: " + String(e)}]);
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

  const fmtDate = (v) => v ? new Date(v).toLocaleDateString("ru-RU") : "—";
  const fmtNum = (v) => v != null ? Number(v).toFixed(2) : "—";

  const verdictLabel = (r) => {
    if (r.is_loophole === true) return "лазейка";
    if (r.is_loophole === false) return "не лазейка";
    return "не размечено";
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
              : view === "queue" ? "Очередь верификации"
              : view === "admin" ? "Администрирование"
              : "Лазейки и уязвимости в продуктах банка"}
          </h1>
          <div className="lp-header-actions">
            {view === "ai_research" && (
              <button className="lp-btn" onClick={() => setChatOpen(o => !o)}>
                {chatOpen ? "Скрыть чат" : "Открыть чат"}
              </button>
            )}
            {view === "catalog" && (<>
            <span className="lp-count-badge">
              {loading ? "…" : sortedRecords.length} записей
            </span>
            <button className="lp-btn" onClick={() => setParsersOpen(true)}
                    disabled={!workspaceId} title="Управление парсерами">
              ⚙ Парсеры
            </button>
            <button className="lp-btn lp-btn-primary" onClick={exportCSV}
                    disabled={loading || sortedRecords.length === 0}
                    title="Выгрузить выделенные записи в CSV (не более 10000)">
              ⬇ CSV
            </button>
            </>)}
            {view !== "ai_research" && (
            <button className="lp-btn"
                    onClick={view === "queue" ? loadQueue
                      : view === "admin" ? loadAdmin : loadRecords}
                    disabled={loading || queueLoading || adminLoading}>
              {(loading || queueLoading || adminLoading) ? "…" : "↻ Обновить"}
            </button>
            )}
          </div>
        </header>

        {/* Рабочие контексты, доступные principal (список пришёл с сервера) */}
        <nav className="lp-context-nav" aria-label="Рабочие контексты">
          {authz.contexts.map(c => {
            const active = c.id === view;
            return (
              <button key={c.id} type="button"
                      className={"lp-btn" + (active ? " lp-btn-primary" : "")}
                      onClick={() => openContext(c.id)}>
                {c.title}
              </button>
            );
          })}
        </nav>

        {view === "catalog" && (<>
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
            <label htmlFor="lp-filter-from">Период сбора</label>
            <div className="lp-period">
              <input id="lp-filter-from" type="date" value={fFrom}
                     onChange={e => setFFrom(e.target.value)}/>
              <span>—</span>
              <label className="lp-sr-only" htmlFor="lp-filter-to">Конец периода сбора</label>
              <input id="lp-filter-to" type="date" value={fTo}
                     onChange={e => setFTo(e.target.value)}/>
            </div>
          </div>
          <div className="lp-filter">
            <label htmlFor="lp-filter-verdict">Вердикт</label>
            <select id="lp-filter-verdict" value={fVerdict} onChange={e => setFVerdict(e.target.value)}>
              <option value="all">все</option>
              <option value="loophole">лазейка</option>
              <option value="not">не лазейка</option>
            </select>
          </div>
          <div className="lp-filter">
            <label htmlFor="lp-filter-status">Статус</label>
            <select id="lp-filter-status" value={fStatus} onChange={e => setFStatus(e.target.value)}>
              <option value="">любой</option>
              <option value="new">Новый</option>
              <option value="classified">Классифицирован</option>
              <option value="exported">Выгружен</option>
            </select>
          </div>
          <div className="lp-filter lp-filter-reset">
            <button className="lp-btn" onClick={resetFilters}>Сбросить</button>
          </div>
        </div>

        {/* Панель массовой маркировки */}
        {selected.size > 0 && (
          <div className="lp-mark-panel">
            <div className="lp-mark-meta">
              <span className="lp-mark-eyebrow">Массовая маркировка</span>
              <span className="lp-mark-count">{selected.size}</span>
            </div>
            <label className="lp-sr-only" htmlFor="lp-bulk-comment">Комментарий аудитора</label>
            <input id="lp-bulk-comment" type="text" className="lp-mark-comment" value={bulkComment}
                   onChange={e => setBulkComment(e.target.value)}
                   placeholder="Комментарий аудитора (необязательно)"/>
            <div className="lp-mark-actions">
              <button className="lp-mark-btn lp-mark-btn-bad" disabled={markBusy}
                      onClick={async () => {
                        const ok = await markVerdict([...selected], true, bulkComment.trim());
                        if (ok) setBulkComment("");
                      }}>
                <span className="lp-verdict-dot"></span>Лазейка
              </button>
              <button className="lp-mark-btn lp-mark-btn-ok" disabled={markBusy}
                      onClick={async () => {
                        const ok = await markVerdict([...selected], false, bulkComment.trim());
                        if (ok) setBulkComment("");
                      }}>
                <span className="lp-verdict-dot"></span>Обычный запрос
              </button>
              <button className="lp-btn lp-btn-sm" disabled={markBusy}
                      onClick={() => setSelected(new Set())}>
                Снять выбор
              </button>
            </div>
          </div>
        )}

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
                  <th className="lp-col-narrow2" {...sortableThProps("trust_score")}>
                    <button type="button" className="lp-sort-button"
                            onClick={() => toggleSort("trust_score")}>
                      Надёжность{sortArrow("trust_score")}
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
                  <th className="lp-col-narrow1" {...sortableThProps("collected_at")}>
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
                      <td className="lp-col-narrow2">{fmtNum(r.trust_score)}</td>
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
                        <span className="lp-status">{r.status || "—"}</span>
                      </td>
                      <td className="lp-cell-date lp-col-narrow1">{fmtDate(r.collected_at)}</td>
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
        </>)}

        {/* ── AI-исследование: работа идёт в панели чата, общая база и очередь
               на этой поверхности не показываются ─────────────────────────── */}
        {view === "ai_research" && (
          <div className="lp-research-surface">
            <h2>Исследование выполняется в чате аналитика</h2>
            <p>
              Сформулируйте задачу в панели чата: аналитик уточнит параметры,
              выполнит исследование и покажет ход выполнения и результаты
              с первоисточниками.
              {!chatOpen && " Панель скрыта — откройте её кнопкой «Открыть чат» в заголовке."}
            </p>
          </div>
        )}

        {/* ── Очередь верификации ЦК КС (fail-closed при 403/отзыве роли) ── */}
        {view === "queue" && (
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
                <table className="lp-table">
                  <thead>
                    <tr>
                      <th>Запись</th>
                      <th className="lp-col-narrow2">Банк</th>
                      <th className="lp-col-narrow2">Доверие</th>
                      <th>Статус</th>
                      <th className="lp-col-narrow1">Собрано</th>
                      <th className="lp-col-narrow1">URL</th>
                    </tr>
                  </thead>
                  <tbody>
                    {queueRecords.map(r => (
                      <React.Fragment key={r.record_id}>
                        <tr>
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
                              </button>
                            </div>
                          </td>
                          <td className="lp-col-narrow2">{r.bank_slug || "—"}</td>
                          <td className="lp-col-narrow2">{fmtNum(r.verdict_confidence)}</td>
                          <td><span className="lp-status">{r.status || "—"}</span></td>
                          <td className="lp-cell-date lp-col-narrow1">{fmtDate(r.collected_at)}</td>
                          <td className="lp-cell-url lp-col-narrow1">
                            {r.url ? <a href={r.url} target="_blank" rel="noopener noreferrer"
                                       onClick={e => e.stopPropagation()}>открыть ↗</a> : "—"}
                          </td>
                        </tr>
                        {expanded.has(r.record_id) && (
                          <tr className="lp-content-row">
                            <td id={`lp-record-details-${r.record_id}`} colSpan={6}>
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
          )
        )}
        {/* ── Администрирование (story 1.5): роль ЦК КС, статус Telegram-целей,
               сводный обезличенный аудит. Черновики исследований, очередь,
               каталог и технические payload на этой поверхности не показываются ── */}
        {view === "admin" && (
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

              {/* Статус Telegram-целей: цель + операционный статус парсера */}
              <section className="lp-admin-section" aria-labelledby="lp-admin-tg-title">
                <h2 id="lp-admin-tg-title">Статус Telegram-целей</h2>
                {!adminTargets || adminTargets.length === 0 ? (
                  <div className="lp-empty-state">Telegram-цели не зарегистрированы.</div>
                ) : (
                  <div className="lp-table-wrap">
                    <table className="lp-table">
                      <thead>
                        <tr>
                          <th>Цель</th>
                          <th>Парсер</th>
                          <th>Статус</th>
                          <th className="lp-col-narrow1">Последний запуск</th>
                        </tr>
                      </thead>
                      <tbody>
                        {adminTargets.map(t => (
                          <tr key={t.target}>
                            <td>{t.target}</td>
                            <td>{t.parser_name || `Парсер #${t.parser_id}`}</td>
                            <td><span className="lp-status">{t.status || "—"}</span></td>
                            <td className="lp-cell-date lp-col-narrow1">{fmtDate(t.last_run_at)}</td>
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
          )
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
              <span className={"lp-dot " + (chatLoading ? "lp-dot-busy" : "lp-dot-online")}></span>
              {chatLoading ? "думает…" : "готов"}
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
                    const optionInputId = `lp-question-${q.id}-${i}`;
                    return (
                      <label key={i} htmlFor={optionInputId}
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
                          {opt.recommended ? <span className="lp-option-rec"> рекомендуем</span> : null}
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
                      value={a.other || ""}
                      onChange={e => setOtherText(q.id, e.target.value)}
                      placeholder="Опишите иначе…"
                    />
                  </div>
                )}
                <div className="lp-question-actions">
                  <button className="lp-btn lp-btn-primary lp-btn-sm"
                          disabled={clarifySubmitting}
                          onClick={submitAnswers}>
                    {clarifySubmitting ? "Отправляю…" : "Ответить"}
                  </button>
                </div>
              </div>
            </div>
          );
        })()}

        <div className="lp-chat-input-area">
          <label className="lp-sr-only" htmlFor="lp-chat-input">Сообщение аналитику</label>
          <textarea id="lp-chat-input"
            ref={chatInputRef}
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
            type="button"
            aria-label="Отправить сообщение"
            onClick={() => sendChat()}
            disabled={chatLoading || !workspaceId || !chatInput.trim() || (pendingQuestions && pendingQuestions.length > 0)}
          >
            {chatLoading ? "…" : "➤"}
          </button>
        </div>
      </aside>)}

      {/* ── Модал парсеров ──────────────────────────────────────────────────── */}
      {parsersOpen && (
        <div className="lp-parsers-modal">
          <button type="button" className="lp-modal-backdrop"
                  aria-label="Закрыть диалог" tabIndex={-1}
                  onClick={() => setParsersOpen(false)} />
          <div className="lp-parsers-dialog" ref={parsersDialogRef}
               role="dialog" aria-modal="true" aria-labelledby="lp-parsers-title">
            <div className="lp-parsers-header">
              <h2 id="lp-parsers-title">Парсеры</h2>
              <button className="lp-btn" ref={parsersCloseRef} aria-label="Закрыть"
                      onClick={() => setParsersOpen(false)}>✕</button>
            </div>

            <div className="lp-parsers-create">
              <label className="lp-sr-only" htmlFor="lp-parser-query">Источник для нового парсера</label>
              <input id="lp-parser-query"
                type="text"
                value={newParserQuery}
                onChange={e => { setNewParserQuery(e.target.value); setParserError(""); }}
                placeholder="URL ресурса или группа мессенджера (например: https://t.me/group_name)"
                onKeyDown={e => { if (e.key === "Enter") createParser(); }}
              />
              <button className="lp-btn lp-btn-primary"
                      onClick={createParser}
                      disabled={parsersBusy || !newParserQuery.trim()}>
                Создать
              </button>
            </div>
            {parserError && <div className="lp-parser-error">{parserError}</div>}

            <div className="lp-parsers-list">
              {parsersLoading ? (
                <div className="lp-empty-state">Загрузка парсеров…</div>
              ) : parsersError ? (
                <div className="lp-empty-state">
                  <p>Не удалось загрузить парсеры. Проверьте соединение и повторите.</p>
                  <button className="lp-btn" onClick={loadParsers}>Повторить</button>
                </div>
              ) : parsers.length === 0 ? (
                <div className="lp-empty-state">
                  <p>Парсеры не созданы.</p>
                  <button className="lp-btn" onClick={loadParsers}>Сбросить</button>
                </div>
              ) : (
                <>
                  {parsers.map(p => {
                const st = p.last_run && p.last_run.status;
                const fmtDt = (v) => {
                  if (!v) return null;
                  const d = new Date(v);
                  return isNaN(d) ? String(v) : d.toLocaleString("ru-RU");
                };
                return (
                  <div key={p.parser_id} className="lp-parser-row">
                    <div className="lp-parser-info">
                      <div className="lp-parser-name">
                        {p.name || `Парсер #${p.parser_id}`}
                        {p.is_running && <span className="lp-badge lp-badge-run">⏳ выполняется</span>}
                        {!p.is_running && st === "success" && <span className="lp-badge lp-badge-ok">✅ успех</span>}
                        {!p.is_running && st === "error" && <span className="lp-badge lp-badge-err">❌ ошибка</span>}
                        {!p.is_running && st === "empty" && <span className="lp-badge lp-badge-empty">⚪ 0 результатов</span>}
                        {p.needs_attention && <span className="lp-badge lp-badge-attn">🔧 требует вмешательства</span>}
                      </div>
                      {(p.targets && p.targets.length > 0) && (
                        <div className="lp-parser-targets">
                          {p.targets.map((t, i) => {
                            const href = /^https?:\/\//i.test(t) ? t
                              : (t.startsWith("@") ? `https://t.me/${t.slice(1)}` : `https://${t}`);
                            return <a key={i} href={href} target="_blank" rel="noopener noreferrer">{t}</a>;
                          })}
                        </div>
                      )}
                      <div className="lp-parser-meta">
                        <span>источников в БД: {p.records_count ?? 0}</span>
                        {p.last_run && p.last_run.finished_at && (
                          <span> · последний запуск: {fmtDt(p.last_run.finished_at)}</span>
                        )}
                        {p.last_run && st === "success" && (
                          <span> · новых: {p.last_run.items_new}</span>
                        )}
                        {p.auto_enabled && p.next_run_at && (
                          <span> · след. запуск: {fmtDt(p.next_run_at)}</span>
                        )}
                        {p.created_by && <span> · автор: {p.created_by}</span>}
                      </div>

                      {editParserId === p.parser_id && (
                        <div className="lp-parser-edit">
                          <label htmlFor={`lp-parser-name-${p.parser_id}`}>Название
                            <input id={`lp-parser-name-${p.parser_id}`} type="text" value={editForm.name}
                                   onChange={e => setEditForm({...editForm, name: e.target.value})} />
                          </label>
                          <label htmlFor={`lp-parser-cron-${p.parser_id}`}>Расписание (cron)
                            <input id={`lp-parser-cron-${p.parser_id}`} type="text" placeholder="0 5 * * *"
                                   value={editForm.cron_expr}
                                   disabled={!editForm.auto_enabled}
                                   onChange={e => setEditForm({...editForm, cron_expr: e.target.value})} />
                          </label>
                          <label className="lp-parser-edit-toggle" htmlFor={`lp-parser-auto-${p.parser_id}`}>
                            <input id={`lp-parser-auto-${p.parser_id}`} type="checkbox" checked={editForm.auto_enabled}
                                   onChange={e => setEditForm({...editForm, auto_enabled: e.target.checked})} />
                            Автозапуск включён
                          </label>
                          {editError && <div className="lp-parser-error">{editError}</div>}
                          <div className="lp-parser-edit-actions">
                            <button className="lp-btn lp-btn-sm lp-btn-primary"
                                    onClick={saveEdit} disabled={parsersBusy}>
                              Сохранить
                            </button>
                            <button className="lp-btn lp-btn-sm"
                                    onClick={() => setEditParserId(null)}>
                              Отмена
                            </button>
                            <button className="lp-btn lp-btn-sm"
                                    onClick={() => healParser(p.parser_id)}
                                    disabled={parsersBusy}>
                              🔧 Анализ и восстановление
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                    <div className="lp-parser-actions">
                      <button className="lp-btn lp-btn-sm"
                              onClick={() => startParser(p.parser_id)}
                              disabled={parsersBusy || p.is_running}>
                        ▶ Запустить
                      </button>
                      <button className="lp-btn lp-btn-sm"
                              onClick={() => stopParser(p.parser_id)}
                              disabled={parsersBusy || !p.is_running}
                              aria-label="Остановить парсер">
                        ■
                      </button>
                      <button className="lp-btn lp-btn-sm"
                              onClick={() => openEdit(p)}
                              disabled={parsersBusy}>
                        Редактировать
                      </button>
                      <button className="lp-btn lp-btn-sm"
                              onClick={() => setDeleteConfirm(p)}
                              disabled={parsersBusy || p.is_running}>
                        Удалить
                      </button>
                    </div>
                  </div>
                );
                  })}
                </>
              )}
            </div>

            {logPanel && (
              <div className="lp-log-panel">
                <div className="lp-log-header">
                  <span>Лог запуска #{logPanel.runId}</span>
                  {logPanel.done && (
                    <span className="lp-log-done">
                      {logPanel.done.status}
                      {logPanel.done.items_new != null && ` · новых: ${logPanel.done.items_new}`}
                    </span>
                  )}
                  <button type="button" className="lp-btn lp-btn-sm"
                          onClick={() => { closeLogEs(); setLogPanel(null); }}
                          aria-label="Закрыть журнал запуска">✕</button>
                </div>
                <pre className="lp-log-body" ref={logRef}>
                  {logPanel.lines.join("\n")}
                </pre>
              </div>
            )}
          </div>
        </div>
      )}

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
          {toast.text}
        </div>
      )}
    </div>
  );
}

const root = ReactDOM.createRoot(document.getElementById("loophole-root"));
root.render(<LoopholeApp />);