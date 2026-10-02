/* Собрано из loophole.jsx (sha256 7a4e023a9e632c04d44eaf911c459b6e75a67cbca4f6dfc174a977d78f13a9bc). Не править вручную: node scripts/build_loophole_js.mjs */
/* loophole.jsx — вкладка «Уязвимости» в системе AuditLens: база (сводка, фильтры,
   список и карточка записи, Excel, аудит-дела), исследование агента одной
   колонкой, очередь решений ЦК КС и панель «Доступ». Права решает сервер. */
const {
  useState,
  useEffect,
  useRef,
  useCallback,
  useMemo
} = React;
const API = "/api/loophole";

// Размер страницы общей базы (дублирует верхнюю границу limit на бэкенде).
const PAGE_SIZE = 50;

// Названия банков вместо кодов bank_slug — только отображение: фильтры и API
// по-прежнему работают с кодом. Неизвестный код показывается как есть.
const BANK_NAMES = {
  sberbank: "Сбербанк",
  sber: "Сбербанк",
  vtb: "ВТБ",
  alfabank: "Альфа-Банк",
  alfa: "Альфа-Банк",
  tbank: "Т-Банк",
  tinkoff: "Т-Банк",
  gazprombank: "Газпромбанк",
  gpb: "Газпромбанк",
  raiffeisen: "Райффайзенбанк",
  rosbank: "Росбанк",
  sovcombank: "Совкомбанк",
  mtsbank: "МТС Банк",
  mts: "МТС Банк",
  pochtabank: "Почта Банк",
  otkritie: "Открытие",
  psb: "ПСБ",
  rshb: "Россельхозбанк",
  domrf: "Банк ДОМ.РФ",
  ozon: "Озон Банк",
  ozonbank: "Озон Банк",
  yandex: "Яндекс Банк",
  uralsib: "Уралсиб",
  akbars: "Ак Барс",
  mkb: "МКБ",
  homecredit: "Хоум Банк",
  renaissance: "Ренессанс Банк",
  all: "Все банки",
  generic: "Банк не указан",
  other: "Другие банки"
};
function bankName(slug) {
  const value = String(slug || "").trim();
  return value ? BANK_NAMES[value.toLowerCase()] || value : "—";
}
// Коды, которыми сборщик помечает «банк не определён»: в интерфейсе — пусто.
const UNKNOWN_BANKS = new Set(["", "all", "generic", "other"]);
function knownBank(slug) {
  const value = String(slug || "").trim();
  return UNKNOWN_BANKS.has(value.toLowerCase()) ? null : bankName(value);
}
// Сбер — объект аудита: выделяется зелёным, как во всей системе AuditLens.
function bankClass(slug) {
  return /^sber/i.test(String(slug || "")) ? "lp-bank lp-bank-sber" : "lp-bank";
}

// Сводный аудит: события и решения по-русски; неизвестный код — как есть.
const AUDIT_ACTION_LABELS = {
  role_grant: "Назначение эксперта ЦК КС",
  role_assign: "Назначение эксперта ЦК КС",
  role_revoke: "Отзыв роли ЦК КС",
  queue_access: "Открытие очереди верификации",
  verification_decide: "Решение ЦК КС",
  mark_verdict: "Ручной вердикт",
  membership_check: "Проверка доступа к модулю",
  admin_roles_read: "Просмотр ролей",
  admin_audit_read: "Просмотр сводного аудита",
  parser_development_request_create: "Заявка на парсер"
};
const AUDIT_DECISION_LABELS = {
  allow: "разрешено",
  deny: "отказано"
};

// Фазы, которые реально сообщает nanobot-пайплайн, включая финальное done.
// Пользователь видит только русские подписи, протокольные ключи не меняются.
const PHASES = ["clarify", "execute", "answer", "done"];
const SUBAGENT_STAGES = {
  queued: "Ожидает свободного исследователя",
  searching: "Поиск материалов",
  classifying: "Анализ описаний",
  completed: "Завершено",
  failed: "Не удалось завершить",
  cancelled: "Прервано"
};
const SUBAGENT_CATEGORIES = {
  loophole: "Лазейка",
  fraud: "Признаки мошенничества",
  irrelevant: "Не относится",
  insufficient_data: "Недостаточно данных"
};
const SUBAGENT_CONTENT_TYPES = {
  article: "Статья",
  post: "Пост",
  comment: "Комментарий",
  unknown: "Материал"
};
const SUBAGENT_ERRORS = {
  timeout: "Не хватило времени на поиск и анализ.",
  search_error: "Поисковик временно недоступен.",
  model_error: "Младшая модель не смогла завершить ответ.",
  invalid_response: "Младшая модель вернула некорректную разметку материалов."
};
function subagentSourceHref(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch {
    return null;
  }
}
function acceptSubagentEvent(value) {
  if (!value || !/^subagent-[1-6](?:-retry-[12])?$/.test(value.id) || !Object.hasOwn(SUBAGENT_STAGES, value.status) || !Number.isInteger(value.total) || !Number.isInteger(value.completed) || value.completed < 0 || value.total < value.completed || value.total > 8) return null;
  return {
    id: value.id,
    status: value.status,
    title: String(value.title || "").slice(0, 200),
    total: value.total,
    completed: value.completed,
    retry_of: /^subagent-[1-6](?:-retry-1)?$/.test(value.retry_of) ? value.retry_of : null,
    error_code: Object.hasOwn(SUBAGENT_ERRORS, value.error_code) ? value.error_code : null,
    items: (Array.isArray(value.items) ? value.items : []).slice(0, 8).filter(item => item && Object.hasOwn(SUBAGENT_CATEGORIES, item.category)).map(item => ({
      title: String(item.title || "Материал").slice(0, 200),
      reason: String(item.reason || "").slice(0, 300),
      category: item.category,
      content_type: Object.hasOwn(SUBAGENT_CONTENT_TYPES, item.content_type) ? item.content_type : "unknown",
      url: subagentSourceHref(item.url)
    }))
  };
}
function SubagentCards({
  agents
}) {
  if (!agents.length) return null;
  return /*#__PURE__*/React.createElement("section", {
    className: "lp-subagent-list",
    "aria-label": "\u041C\u043B\u0430\u0434\u0448\u0438\u0435 \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u0442\u0435\u043B\u0438"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-subtasks-title"
  }, "\u041C\u043B\u0430\u0434\u0448\u0438\u0435 \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u0442\u0435\u043B\u0438"), /*#__PURE__*/React.createElement("p", {
    className: "lp-subagent-note"
  }, "\u041F\u0440\u0435\u0434\u0432\u0430\u0440\u0438\u0442\u0435\u043B\u044C\u043D\u044B\u0439 \u043E\u0442\u0431\u043E\u0440 \u043F\u043E \u043E\u043F\u0438\u0441\u0430\u043D\u0438\u044F\u043C"), agents.map(agent => {
    const busy = ["queued", "searching", "classifying"].includes(agent.status);
    return /*#__PURE__*/React.createElement("article", {
      key: agent.id,
      className: "lp-subagent-card lp-subagent-" + agent.status,
      "aria-busy": busy
    }, /*#__PURE__*/React.createElement("div", {
      className: "lp-subagent-heading"
    }, /*#__PURE__*/React.createElement("span", {
      className: "lp-subagent-indicator" + (busy ? " is-active" : ""),
      "aria-hidden": "true"
    }), /*#__PURE__*/React.createElement("strong", null, "\u0418\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u0442\u0435\u043B\u044C ", agent.id.split("-")[1], agent.retry_of ? ` · замена ${agent.id.split("-")[3]}` : ""), /*#__PURE__*/React.createElement("span", {
      role: "status",
      className: "lp-subagent-status"
    }, SUBAGENT_STAGES[agent.status])), /*#__PURE__*/React.createElement("div", {
      className: "lp-subagent-query"
    }, agent.title), agent.retry_of && /*#__PURE__*/React.createElement("p", {
      className: "lp-subagent-note"
    }, "\u041F\u0440\u043E\u0434\u043E\u043B\u0436\u0430\u0435\u0442 \u043D\u0435\u043E\u0431\u0440\u0430\u0431\u043E\u0442\u0430\u043D\u043D\u044B\u0435 \u043C\u0430\u0442\u0435\u0440\u0438\u0430\u043B\u044B \u043F\u0440\u0435\u0434\u044B\u0434\u0443\u0449\u0435\u0433\u043E \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u0442\u0435\u043B\u044F."), /*#__PURE__*/React.createElement("div", {
      className: "lp-subagent-steps",
      "aria-hidden": "true"
    }, /*#__PURE__*/React.createElement("span", {
      className: "is-reached"
    }, "\u041F\u043E\u0438\u0441\u043A"), /*#__PURE__*/React.createElement("span", null, "\u2192"), /*#__PURE__*/React.createElement("span", {
      className: agent.total > 0 ? "is-reached" : ""
    }, "\u0410\u043D\u0430\u043B\u0438\u0437"), /*#__PURE__*/React.createElement("span", null, "\u2192"), /*#__PURE__*/React.createElement("span", {
      className: agent.status === "completed" ? "is-reached" : ""
    }, "\u0420\u0435\u0437\u0443\u043B\u044C\u0442\u0430\u0442")), agent.total > 0 && /*#__PURE__*/React.createElement("div", {
      className: "lp-subagent-count"
    }, "\u0420\u0430\u0437\u043C\u0435\u0447\u0435\u043D\u043E ", agent.completed, " \u0438\u0437 ", agent.total, " \u043C\u0430\u0442\u0435\u0440\u0438\u0430\u043B\u043E\u0432"), agent.status === "failed" && /*#__PURE__*/React.createElement("p", {
      className: "lp-subagent-note"
    }, SUBAGENT_ERRORS[agent.error_code] || "Отбор неполный. Основной аналитик получил информацию о сбое."), agent.status === "completed" && agent.total === 0 && /*#__PURE__*/React.createElement("p", {
      className: "lp-subagent-note"
    }, "\u041F\u043E \u044D\u0442\u043E\u043C\u0443 \u0437\u0430\u043F\u0440\u043E\u0441\u0443 \u043C\u0430\u0442\u0435\u0440\u0438\u0430\u043B\u044B \u043D\u0435 \u043D\u0430\u0439\u0434\u0435\u043D\u044B."), agent.items.length > 0 && /*#__PURE__*/React.createElement("details", {
      className: "lp-subagent-results",
      open: true
    }, /*#__PURE__*/React.createElement("summary", null, "\u041F\u0440\u0435\u0434\u0432\u0430\u0440\u0438\u0442\u0435\u043B\u044C\u043D\u044B\u0435 \u043C\u0435\u0442\u043A\u0438 \xB7 ", agent.items.length), agent.items.map((item, index) => /*#__PURE__*/React.createElement("div", {
      className: "lp-subagent-item",
      key: index
    }, /*#__PURE__*/React.createElement("div", {
      className: "lp-subagent-item-meta"
    }, /*#__PURE__*/React.createElement("span", {
      className: "lp-subagent-label lp-subagent-label-" + item.category
    }, SUBAGENT_CATEGORIES[item.category]), /*#__PURE__*/React.createElement("span", null, SUBAGENT_CONTENT_TYPES[item.content_type])), item.url ? /*#__PURE__*/React.createElement("a", {
      href: item.url,
      target: "_blank",
      rel: "noopener noreferrer"
    }, item.title) : /*#__PURE__*/React.createElement("span", null, item.title), /*#__PURE__*/React.createElement("p", null, item.reason))), /*#__PURE__*/React.createElement("p", {
      className: "lp-subagent-note"
    }, "\u0412\u044B\u0432\u043E\u0434\u044B \u0442\u0440\u0435\u0431\u0443\u044E\u0442 \u043F\u0440\u043E\u0432\u0435\u0440\u043A\u0438 \u043F\u0435\u0440\u0432\u043E\u0438\u0441\u0442\u043E\u0447\u043D\u0438\u043A\u043E\u0432.")));
  }));
}
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
  return null;
}
function publicChatErrorMessage(value) {
  const message = String(value || "").trim();
  if (!message || /error calling llm|connection error|apiconnectionerror|connecterror/i.test(message) || /http 5\d\d|failed to fetch|networkerror/i.test(message)) {
    return "Аналитик временно недоступен. Повторите запрос через несколько секунд.";
  }
  return message;
}

// ── Markdown-рендерер результата исследования ───────────────────────────────
// Зеркало python-рендерера loophole/markdown_render.py (PDF-экспорт): заголовки,
// списки, таблицы, цитаты, код-блоки, ссылки. Весь вход сначала экранируется —
// в markdown попадает недоверенный вывод LLM (stored XSS через <img onerror=…>).

function lpEscAttr(value) {
  return String(value == null ? "" : value).replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function lpInlineMarkdown(text) {
  return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  // markdown-ссылки [текст](url) — только http(s), URL через lpEscAttr.
  .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_, label, url) => `<a href="${lpEscAttr(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
  // __жирный__ / _курсив_ — только на границах слова; JS \w без кириллицы,
  // поэтому класс слова задан явно.
  .replace(/(^|[^A-Za-zА-Яа-яЁё0-9_])__([^_]+?)__(?![A-Za-zА-Яа-яЁё0-9])/g, "$1<strong>$2</strong>").replace(/\*(.+?)\*/g, "<em>$1</em>").replace(/(^|[^A-Za-zА-Яа-яЁё0-9_])_([^_]+?)_(?![A-Za-zА-Яа-яЁё0-9])/g, "$1<em>$2</em>").replace(/`([^`]+)`/g, "<code>$1</code>").replace(/~~(.+?)~~/g, "<s>$1</s>");
}
function SafeMarkdown({
  content
}) {
  const lines = String(content || "").split(/\r?\n/);
  const blocks = [];
  let list = [];
  let listOrdered = false;
  let tableHead = null;
  let tableRows = [];
  let quote = [];
  let code = null;
  const flushList = () => {
    if (!list.length) return;
    const Tag = listOrdered ? "ol" : "ul";
    blocks.push(/*#__PURE__*/React.createElement(Tag, {
      key: `l-${blocks.length}`
    }, list.map((item, i) => /*#__PURE__*/React.createElement("li", {
      key: i,
      dangerouslySetInnerHTML: {
        __html: lpInlineMarkdown(item)
      }
    }))));
    list = [];
    listOrdered = false;
  };
  const flushTable = () => {
    if (!tableHead) return;
    blocks.push(/*#__PURE__*/React.createElement("div", {
      key: `t-${blocks.length}`,
      className: "lp-md-table-wrap"
    }, /*#__PURE__*/React.createElement("table", null, /*#__PURE__*/React.createElement("thead", null, /*#__PURE__*/React.createElement("tr", null, tableHead.map((cell, i) => /*#__PURE__*/React.createElement("th", {
      key: i,
      dangerouslySetInnerHTML: {
        __html: lpInlineMarkdown(cell)
      }
    })))), /*#__PURE__*/React.createElement("tbody", null, tableRows.map((row, i) => /*#__PURE__*/React.createElement("tr", {
      key: i
    }, row.map((cell, j) => /*#__PURE__*/React.createElement("td", {
      key: j,
      dangerouslySetInnerHTML: {
        __html: lpInlineMarkdown(cell)
      }
    }))))))));
    tableHead = null;
    tableRows = [];
  };
  const flushQuote = () => {
    if (!quote.length) return;
    blocks.push(/*#__PURE__*/React.createElement("blockquote", {
      key: `q-${blocks.length}`,
      dangerouslySetInnerHTML: {
        __html: quote.map(lpInlineMarkdown).join("<br>")
      }
    }));
    quote = [];
  };
  const flushBlocks = () => {
    flushList();
    flushTable();
    flushQuote();
  };
  const flushCode = () => {
    if (code === null) return;
    // React сам экранирует children — код выводим как текст.
    blocks.push(/*#__PURE__*/React.createElement("pre", {
      key: `c-${blocks.length}`
    }, /*#__PURE__*/React.createElement("code", null, code.join("\n"))));
    code = null;
  };
  lines.forEach((raw, idx) => {
    if (code !== null) {
      if (/^\s*```/.test(raw)) flushCode();else code.push(raw);
      return;
    }
    if (/^\s*```/.test(raw)) {
      flushBlocks();
      code = [];
      return;
    }
    const quoteMatch = /^>\s?(.*)$/.exec(raw);
    if (quoteMatch) {
      flushList();
      flushTable();
      quote.push(quoteMatch[1]);
      return;
    }
    flushQuote();
    const line = raw.trim();
    if (line.startsWith("|")) {
      const cells = line.split("|").map(cell => cell.trim()).slice(1, -1);
      if (/^[-:\s|]+$/.test(line.replace(/\|/g, ""))) return;
      flushList();
      if (!tableHead) tableHead = cells;else tableRows.push(cells);
      return;
    }
    flushTable();
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      flushList();
      const Tag = `h${Math.min(heading[1].length + 2, 6)}`;
      blocks.push(/*#__PURE__*/React.createElement(Tag, {
        key: `h-${idx}`,
        dangerouslySetInnerHTML: {
          __html: lpInlineMarkdown(heading[2])
        }
      }));
      return;
    }
    if (/^---+$/.test(line)) {
      flushList();
      blocks.push(/*#__PURE__*/React.createElement("hr", {
        key: `hr-${idx}`
      }));
      return;
    }
    const ordered = /^\d+\.\s+(.+)$/.exec(raw);
    if (ordered) {
      if (list.length && !listOrdered) flushList();
      listOrdered = true;
      list.push(ordered[1]);
      return;
    }
    const bullet = /^[-*•]\s+(.+)$/.exec(raw);
    if (bullet) {
      if (list.length && listOrdered) flushList();
      listOrdered = false;
      list.push(bullet[1]);
      return;
    }
    flushList();
    if (!line) return;
    blocks.push(/*#__PURE__*/React.createElement("p", {
      key: `p-${idx}`,
      dangerouslySetInnerHTML: {
        __html: lpInlineMarkdown(raw)
      }
    }));
  });
  flushCode();
  flushBlocks();
  return /*#__PURE__*/React.createElement("div", {
    className: "lp-safe-markdown"
  }, blocks);
}

// ── Активный слой: focus-trap / Escape / возврат фокуса (story 1.4) ─────────
// Общий механизм для модалок и off-canvas панели чата. Фокус циклирует внутри
// активного слоя, Escape закрывает его, после закрытия фокус возвращается на
// контрол, открывший слой. Стек гарантирует, что клавиатурные события
// обрабатывает только верхний слой (модалка подтверждения поверх модалки
// парсеров не закрывает обе сразу).
const FOCUSABLE_SEL = 'a[href], button:not([disabled]), input:not([disabled]), ' + 'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
const _lpLayerStack = [];
function useFocusLayer(active, containerRef, onClose, initialFocusRef, restoreFallbackRef) {
  const openerRef = useRef(null);
  // onClose храним в ref, обновляемом каждый рендер: эффект ниже живёт с
  // deps [active] и иначе держал бы устаревшее замыкание.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });
  useEffect(() => {
    if (!active) return undefined;
    const id = {};
    _lpLayerStack.push(id);
    openerRef.current = document.activeElement;
    const node = containerRef.current;
    const initial = node && (initialFocusRef && initialFocusRef.current || node.querySelector(FOCUSABLE_SEL));
    if (initial) initial.focus();
    const onKey = e => {
      if (_lpLayerStack[_lpLayerStack.length - 1] !== id) return; // не верхний слой
      if (e.key === "Escape") {
        e.preventDefault();
        onCloseRef.current();
        return;
      }
      if (e.key !== "Tab" || !node) return;
      const items = [...node.querySelectorAll(FOCUSABLE_SEL)].filter(el => el.getClientRects().length > 0);
      if (!items.length) {
        e.preventDefault();
        return;
      }
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
      const atEdge = e.shiftKey ? cur === first : cur === last;
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
      const enabled = el => el && document.contains(el) && !el.matches(":disabled");
      // Подтверждённое действие может отключить opener до cleanup (например,
      // «Удалить» при сетевом запросе). Тогда возвращаем фокус в родительский
      // слой на стабильный enabled-контрол, а не теряем его на document.body.
      const restoreTarget = enabled(opener) ? opener : enabled(fallback) ? fallback : null;
      if (restoreTarget) restoreTarget.focus();
    };
  }, [active]); // eslint-disable-line react-hooks/exhaustive-deps
}

// ── Вкладка «Уязвимости» в системе AuditLens: общие элементы интерфейса ─────
// Иконки — inline SVG (правило системы: никаких эмодзи), 16×16, штрих 1.5.
const LP_ICONS = {
  search: '<circle cx="7" cy="7" r="4.5"/><path d="m10.5 10.5 3 3"/>',
  dl: '<path d="M8 2.5v8M4.5 7 8 10.5 11.5 7M3 13.5h10"/>',
  plus: '<path d="M8 3v10M3 8h10"/>',
  users: '<circle cx="6" cy="5.5" r="2.5"/><path d="M1.8 13.5c.5-2.3 2.2-3.5 4.2-3.5s3.7 1.2 4.2 3.5"/><path d="M10.8 3.3a2.3 2.3 0 0 1 0 4.4M12.2 10.2c1 .5 1.7 1.6 2 3.3"/>',
  ext: '<path d="M9.5 2.5h4v4M13.5 2.5 7.5 8.5M12 9.5v3a1 1 0 0 1-1 1H3.5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h3"/>',
  left: '<path d="m10 3.5-4.5 4.5 4.5 4.5"/>',
  right: '<path d="m6 3.5 4.5 4.5L6 12.5"/>',
  up: '<path d="M3.5 10 8 5.5l4.5 4.5"/>',
  down: '<path d="M3.5 6 8 10.5 12.5 6"/>',
  check: '<path d="m3.2 8.4 3 3 6.6-6.6"/>',
  x: '<path d="M4 4l8 8M12 4l-8 8"/>',
  case: '<rect x="2" y="5" width="12" height="8.5" rx="1.5"/><path d="M5.5 5V3.5a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1V5M2 9h12"/>',
  info: '<circle cx="8" cy="8" r="6"/><path d="M8 7.2v4M8 4.9v.1"/>',
  clock: '<circle cx="8" cy="8" r="6"/><path d="M8 4.8V8l2.2 1.4"/>',
  alert: '<path d="M8 2.2 14.3 13H1.7Z"/><path d="M8 6.5v3M8 11.2v.1"/>',
  spark: '<path d="M8 1.8c.4 2.9 1.6 4.2 4.4 4.6-2.8.4-4 1.7-4.4 4.6-.4-2.9-1.6-4.2-4.4-4.6C6.4 6 7.6 4.7 8 1.8Z"/><path d="M12.5 10.5c.2 1.2.7 1.8 1.8 2-1.1.2-1.6.8-1.8 2-.2-1.2-.7-1.8-1.8-2 1.1-.2 1.6-.8 1.8-2Z"/>',
  shield: '<path d="M8 1.8 13 3.6v4.1c0 3-2.1 5.4-5 6.5-2.9-1.1-5-3.5-5-6.5V3.6Z"/>',
  send: '<path d="M8 13V3.5M3.8 7.7 8 3.5l4.2 4.2"/>',
  doc: '<path d="M4.2 1.8h5.3l3 3v8.4a1 1 0 0 1-1 1H4.2a1 1 0 0 1-1-1V2.8a1 1 0 0 1 1-1Z"/><path d="M9.5 1.8v3h3M5.6 8.5h4.8M5.6 11h3.2"/>',
  filter: '<path d="M2.5 3.5h11l-4.2 5v4l-2.6 1v-5Z"/>',
  hist: '<path d="M2.5 8a5.5 5.5 0 1 0 1.6-3.9"/><path d="M2.5 2.5v3h3M8 5v3l2 1.3"/>',
  share: '<path d="M6.2 9.8 9.8 6.2"/><path d="M8.6 4.4 9.9 3a2.6 2.6 0 0 1 3.7 3.7l-1.4 1.3M7.4 11.6 6.1 13a2.6 2.6 0 0 1-3.7-3.7l1.4-1.3"/>',
  inbox: '<path d="M2 9.5 3.8 3.5h8.4L14 9.5v3a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1Z"/><path d="M2 9.5h3.5l1 1.5h3l1-1.5H14"/>'
};
function Icon({
  name,
  size = 16,
  className = ""
}) {
  return /*#__PURE__*/React.createElement("svg", {
    className: "lp-ic " + className,
    width: size,
    height: size,
    viewBox: "0 0 16 16",
    "aria-hidden": "true",
    dangerouslySetInnerHTML: {
      __html: LP_ICONS[name] || ""
    }
  });
}

// Классы записи: уязвимость — красный (риск), схема — фиолетовый (--legal),
// не подтверждено — нейтральный, без вердикта — пунктир.
const KIND_LABELS = {
  vulnerability: ["Уязвимость", "neg"],
  fraud_scheme: ["Мошенническая схема", "legal"],
  not_confirmed: ["Не подтверждено", "neu"],
  none: ["Без вердикта", "dash"]
};
const POSITIVE_KINDS = new Set(["vulnerability", "fraud_scheme"]);
function recordKind(r) {
  if (!r) return "none";
  return r.classification || (r.is_loophole === true ? "vulnerability" : r.is_loophole === false ? "not_confirmed" : "none");
}
function KindBadge({
  kind
}) {
  const [label, tone] = KIND_LABELS[kind] || KIND_LABELS.none;
  return /*#__PURE__*/React.createElement("span", {
    className: "lp-kind lp-kind-" + tone
  }, /*#__PURE__*/React.createElement("span", {
    className: "lp-kind-dot"
  }), label);
}
function lpPlural(n, one, few, many) {
  const m10 = n % 10,
    m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}
const fmtInt = n => (Number(n) || 0).toLocaleString("ru-RU");
const pctOf = v => v == null || !Number.isFinite(Number(v)) ? null : Math.max(0, Math.min(100, Math.round(Number(v) * 100)));
const confWord = v => v == null ? "нет оценки" : v >= 0.8 ? "высокая" : v >= 0.6 ? "средняя" : "низкая";
function fmtDay(v) {
  if (!v) return "—";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric"
  });
}
function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

// Подсветка слов поиска в тексте (React-узлы, без innerHTML).
function Hl({
  text,
  q
}) {
  const value = String(text || "");
  const words = String(q || "").trim().toLowerCase().split(/\s+/).filter(w => w.length > 1);
  if (!words.length) return value;
  const re = new RegExp("(" + words.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "gi");
  return value.split(re).map((part, i) => words.includes(part.toLowerCase()) ? /*#__PURE__*/React.createElement("mark", {
    key: i,
    className: "lp-hl"
  }, part) : part);
}
function LoopholeApp() {
  // ── Таблица / фильтры ──────────────────────────────────────────────────────
  const [records, setRecords] = useState([]);
  const [recordsTotal, setRecordsTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(false);
  // Ошибка загрузки записей (story 1.4): отдельная поверхность с «Повторить»,
  // чтобы сбой не маскировался под пустой результат.
  const [recordsError, setRecordsError] = useState(null);
  const recordsRequestRef = useRef(0);
  const [bankOptions, setBankOptions] = useState([]);
  // Фильтры
  const [fText, setFText] = useState("");
  const [fBanks, setFBanks] = useState([]); // выбранные slug
  const [fFrom, setFFrom] = useState("");
  const [fTo, setFTo] = useState("");
  const [fVerification, setFVerification] = useState("all");
  // По умолчанию — только находки: на проде 99% базы — «не подтверждено»,
  // и уязвимости со схемами тонули среди них.
  const [fClassification, setFClassification] = useState("confirmed");
  // Порядок базы — сортирует сервер: сначала новые или по вероятности модели.
  const [fSort, setFSort] = useState("new");
  // Выделение строк
  const [selected, setSelected] = useState(new Set());

  // ── Полный контент записей (ленивая подгрузка) ──────────────────────────
  const [contentCache, setContentCache] = useState({}); // {id: {loading, data, error}}

  // ── Ручная маркировка вердиктов ───────────────────────────────────────────
  const [verdictModal, setVerdictModal] = useState(null); // {record} | null
  const [markComment, setMarkComment] = useState("");
  const [markBusy, setMarkBusy] = useState(false);
  // Единственный toast (story 1.4): {text, kind} — info | success | error.
  const [toast, setToast] = useState(null);
  const toastSeqRef = useRef(0);
  const toastTimerRef = useRef(null);
  const [lastCsvDownload, setLastCsvDownload] = useState(null); // {url, filename}
  const csvUrlRef = useRef(null);

  // ── Чат ────────────────────────────────────────────────────────────────────
  const [chat, setChat] = useState([]);
  const [chatInput, setChatInput] = useState("");
  const [chatLoading, setChatLoading] = useState(false);
  const [workspaceId, setWorkspaceId] = useState(null);
  const chatScrollRef = useRef(null);
  const [researches, setResearches] = useState([]);
  const [researchWorkspace, setResearchWorkspace] = useState(null);
  const [researchReadOnly, setResearchReadOnly] = useState(true);
  const [researchLoading, setResearchLoading] = useState(true);
  const [researchError, setResearchError] = useState("");
  const [historyListError, setHistoryListError] = useState("");
  const [historyListLoading, setHistoryListLoading] = useState(false);
  const [researchActionBusy, setResearchActionBusy] = useState(false);
  const [savedReports, setSavedReports] = useState([]); // ранние отчёты без сообщения
  const [researchShareUrl, setResearchShareUrl] = useState("");
  const [researchDeleteConfirm, setResearchDeleteConfirm] = useState(false);
  const [researchDeleteError, setResearchDeleteError] = useState("");
  const [researchDeleteTarget, setResearchDeleteTarget] = useState(null);
  const researchRequestRef = useRef(0);
  const historyListRequestRef = useRef(0);
  const researchActionRef = useRef(false);
  const researchAccessRef = useRef({
    readOnly: true,
    loading: true
  });
  const researchTargetRef = useRef({
    token: new URLSearchParams(window.location.search).get("share")
  });
  const chatBusyRef = useRef(false);
  const clarifyBusyRef = useRef(false);
  const researchDeleteDialogRef = useRef(null);
  const researchDeleteCancelRef = useRef(null);
  const researchTabRef = useRef(null);

  // ── Авторизация и рабочие контексты (story 1.1) ──────────────────────────
  // authz: null = проверяем доступ, false = отказ (401/403),
  // "error" = сетевая ошибка загрузки контекстов, иначе {contexts, capabilities}.
  const [authz, setAuthz] = useState(null);
  const canMarkVerdict = !!(authz && authz.capabilities && authz.capabilities.can_mark_verdict === true);
  const [contextsRetry, setContextsRetry] = useState(0); // +1 = повторить /contexts
  const [view, setView] = useState("catalog"); // catalog | sources | ai_research | queue | admin
  const [queueRecords, setQueueRecords] = useState([]);
  const [queueSelectedId, setQueueSelectedId] = useState(null);
  const [queueDenied, setQueueDenied] = useState(false);
  const [queueLoading, setQueueLoading] = useState(false);
  const [queueError, setQueueError] = useState(false);
  const [queueTotal, setQueueTotal] = useState(0); // все ждущие решения, без лимита
  const queueSortRef = useRef("old"); // порядок очереди для loadQueue
  const queueRequestRef = useRef(0);
  // ── Администрирование (story 1.5): роль ЦК КС и сводный аудит ──
  const [adminDenied, setAdminDenied] = useState(false);
  const [adminLoading, setAdminLoading] = useState(false);
  const [adminError, setAdminError] = useState(false);
  const [adminRoles, setAdminRoles] = useState(null); // {roles, active_experts, max_experts}
  const [adminAudit, setAdminAudit] = useState(null); // сводный обезличенный аудит
  const [grantName, setGrantName] = useState("");
  const [adminBusy, setAdminBusy] = useState(false);
  // Отзыв роли — модальное подтверждение вместо системного диалога (story 1.4).
  const chatInputRef = useRef(null);
  // Слои с focus-trap (story 1.4): модалки, панель «Доступ», подтверждение удаления.
  const sourcesTabRef = useRef(null);
  const verdictDialogRef = useRef(null);
  const confirmDialogRef = useRef(null);
  const confirmCancelRef = useRef(null);
  // Модальное подтверждение деструктивного удаления парсера (story 1.4) —
  // вместо системного confirm-диалога.
  const [deleteConfirm, setDeleteConfirm] = useState(null); // parser | null

  // ── Новый пайплайн: фазы / подзадачи / уточняющие вопросы ────────────────
  const [phase, setPhase] = useState(null); // текущая фаза
  const [researchActivity, setResearchActivity] = useState(null); // безопасный stage/elapsed из SSE
  const [subtasks, setSubtasks] = useState([]); // [{title, status}]
  const [pendingQuestions, setPendingQuestions] = useState(null); // null | array
  const [pendingQuery, setPendingQuery] = useState(""); // исходный запрос, вызвавший clarify
  const [clarificationToken, setClarificationToken] = useState(null); // одноразовый token сервера
  const [answersByQ, setAnswersByQ] = useState({}); // {qid: {selected:[], other:""}}
  const [clarifySubmitting, setClarifySubmitting] = useState(false); // идёт /clarify/answer
  const [clarifyError, setClarifyError] = useState(""); // inline-ошибка с восстановлением ответа
  const [toolEvents, setToolEvents] = useState([]); // badges tool_call/tool_result
  const [subagents, setSubagents] = useState([]);
  const [findings, setFindings] = useState([]); // находки исследования в общей базе

  // ── Парсеры ───────────────────────────────────────────────────────────────
  const [parsers, setParsers] = useState([]);
  const parsersRequestRef = useRef(0);
  const [parsersLoading, setParsersLoading] = useState(false);
  const [parsersError, setParsersError] = useState(null);
  const [newParserUrl, setNewParserUrl] = useState("");
  const [newParserDescription, setNewParserDescription] = useState("");
  const [parsersBusy, setParsersBusy] = useState(false);
  const [parserError, setParserError] = useState("");
  const [editParserId, setEditParserId] = useState(null); // id открытой формы
  const [editForm, setEditForm] = useState({
    name: "",
    cron_expr: "",
    auto_enabled: false
  });
  const [editError, setEditError] = useState("");
  const [logPanel, setLogPanel] = useState(null); // {parserId, runId, lines, done, error}
  const logRef = useRef(null);
  const logEsRef = useRef(null); // активный EventSource live-лога

  // Закрытие live-лога при размонтировании (EventSource иначе живёт вечно).
  useEffect(() => () => {
    if (logEsRef.current) logEsRef.current.close();
  }, []);

  // Сначала — ТОЛЬКО контексты: никаких запросов данных до авторизации.
  useEffect(() => {
    fetch(`${API}/contexts`).then(r => {
      // Ответ сервера 401/403 — осознанный отказ (deny-экран);
      // сетевая ошибка уходит в catch → «Сервис недоступен».
      if (!r.ok) {
        setAuthz(false);
        return null;
      }
      return r.json();
    }).then(d => {
      if (!d) return;
      const contexts = d.contexts || [];
      setAuthz({
        contexts,
        capabilities: d.capabilities || {}
      });
      setView(current => contexts.some(context => context.id === current) ? current : "catalog");
    }).catch(() => setAuthz("error"));
  }, [contextsRetry]);

  // Загружаем список банков для фильтра — тоже только после авторизации.
  useEffect(() => {
    if (!authz || !authz.contexts) return;
    fetch(`${API}/banks`).then(r => r.json()).then(d => {
      setBankOptions(d.banks || []);
    }).catch(() => {});
  }, [authz]);

  // Загружаем записи.
  const typedTextRef = useRef("");
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
      params.set("classification", fClassification);
      params.set("sort", fSort);
      params.set("limit", String(PAGE_SIZE));
      params.set("offset", String(page * PAGE_SIZE));
      const url = `${API}/catalog${params.toString() ? "?" + params.toString() : ""}`;
      const r = await fetch(url);
      if (requestGeneration !== recordsRequestRef.current) return;
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      if (requestGeneration !== recordsRequestRef.current) return;
      // «Показать ещё» дописывает страницу к уже показанным; повторы
      // (запись сдвинулась между запросами) заменяются свежими данными.
      const incoming = d.records || [];
      setRecords(prev => {
        if (page === 0) return incoming;
        const fresh = new Map(incoming.map(r => [r.record_id, r]));
        const known = new Set(prev.map(r => r.record_id));
        return [...prev.map(r => fresh.get(r.record_id) || r), ...incoming.filter(r => !known.has(r.record_id))];
      });
      setRecordsTotal(Number.isInteger(d.total) ? d.total : (d.records || []).length);
      setRecordsError(null);
    } catch (e) {
      if (requestGeneration !== recordsRequestRef.current) return;
      // Ошибка не маскируется под пустой результат: отдельная поверхность
      // с «Повторить», старые данные не подменяют актуальное состояние.
      if (page > 0) {
        // Уже показанные записи не теряем: откатываем страницу и сообщаем.
        showToast("Не удалось загрузить ещё записи. Повторите.", "error");
        setPage(p => Math.max(0, p - 1));
        return;
      }
      setRecords([]);
      setRecordsTotal(0);
      setRecordsError(String(e));
    } finally {
      if (requestGeneration === recordsRequestRef.current) {
        setLoading(false);
      }
    }
  }, [fText, fBanks, fFrom, fTo, fVerification, fClassification, fSort, page]);
  useEffect(() => {
    if (!authz || !authz.contexts) return undefined;
    // Антидребезг нужен только при наборе текста; клики по фильтрам и первое
    // открытие вкладки загружают сразу.
    const typing = typedTextRef.current !== fText;
    typedTextRef.current = fText;
    const timer = setTimeout(() => loadRecords(), typing ? 350 : 0);
    return () => clearTimeout(timer);
  }, [loadRecords, authz, fText]);

  // Сброс страницы при смене фильтров (выборка начинается с первой страницы).
  useEffect(() => {
    setPage(0);
  }, [fText, fBanks, fFrom, fTo, fVerification, fClassification, fSort]);

  // Выделение сбрасывается при смене выборки; «Показать ещё» его сохраняет.
  useEffect(() => {
    setSelected(new Set());
  }, [fText, fBanks, fFrom, fTo, fVerification, fClassification, fSort]);
  const toggleRow = id => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);else next.add(id);
      return next;
    });
  };

  // Сброс фильтров каталога — действие «Сбросить» (фильтры + пустая выборка).
  const resetFilters = () => {
    setFText("");
    setFBanks([]);
    setFFrom("");
    setFTo("");
    setFVerification("all");
    setFClassification("confirmed");
    setPage(0);
  };

  // ── Выгрузка: подписи и повторное скачивание ──────────────────────────────
  const recordWord = count => {
    const mod100 = count % 100;
    const mod10 = count % 10;
    if (mod100 >= 11 && mod100 <= 14) return "записей";
    if (mod10 === 1) return "запись";
    if (mod10 >= 2 && mod10 <= 4) return "записи";
    return "записей";
  };
  const triggerCsvDownload = download => {
    if (!download) return;
    const a = document.createElement("a");
    a.href = download.url;
    a.download = download.filename;
    a.click();
  };

  // ── Единственный toast (story 1.4): типы info | success | error ──────────
  // opts.undo — кнопка «Отменить» (отложенное решение, отзыв роли),
  // opts.ttl — сколько держать уведомление на экране.
  const showToast = (text, kind = "info", opts = {}) => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    const ttl = opts.ttl || 4000;
    setToast({
      text,
      kind,
      undo: opts.undo || null,
      ttl,
      id: ++toastSeqRef.current
    });
    toastTimerRef.current = setTimeout(() => setToast(null), ttl);
  };

  // История загружается после проверки доступа. Поколение запроса исключает
  // подмену выбранного исследования поздним ответом при быстрых переходах.
  const resetResearch = () => {
    setWorkspaceId(null);
    setResearchWorkspace(null);
    setResearchReadOnly(true);
    researchAccessRef.current = {
      readOnly: true,
      loading: true
    };
    setChat([]);
    setChatInput("");
    setPhase(null);
    setSubtasks([]);
    setResearchActivity(null);
    setPendingQuestions(null);
    setPendingQuery("");
    setClarificationToken(null);
    setAnswersByQ({});
    setClarifyError("");
    setToolEvents([]);
    setSubagents([]);
    setFindings([]);
    setSavedReports([]);
    setResearchShareUrl("");
  };
  const applyResearch = data => {
    if (!data.workspace || !data.workspace.workspace_id) throw new Error("Неверный ответ истории");
    const readOnly = data.read_only !== false;
    setResearchWorkspace(data.workspace);
    setWorkspaceId(data.workspace.workspace_id);
    setResearchReadOnly(readOnly);
    researchAccessRef.current = {
      readOnly,
      loading: false
    };
    setChat(data.messages || []);
    setFindings(Array.isArray(data.findings) ? data.findings : []);
    // Сервер возвращает отчёты по возрастанию; показываем последние первыми.
    const reports = [...(data.reports || [])].reverse();
    setSavedReports(reports);
  };
  const clearSharedLocation = () => {
    const url = new URL(window.location.href);
    if (!url.searchParams.has("share")) return;
    url.searchParams.delete("share");
    window.history.replaceState(null, "", url);
  };
  const loadResearchList = async () => {
    const generation = ++historyListRequestRef.current;
    setHistoryListLoading(true);
    setHistoryListError("");
    try {
      const response = await fetch(`${API}/workspaces`);
      if (!response.ok) throw new Error("Не удалось загрузить историю исследований.");
      const data = await response.json();
      if (!Array.isArray(data.workspaces)) throw new Error("Не удалось загрузить историю исследований.");
      if (generation === historyListRequestRef.current) {
        setResearches(data.workspaces);
        setResearchWorkspace(current => current ? data.workspaces.find(item => item.workspace_id === current.workspace_id) || current : current);
      }
      return data.workspaces;
    } catch (error) {
      if (generation === historyListRequestRef.current) {
        setHistoryListError("Не удалось загрузить историю исследований. Повторите запрос.");
      }
      return null;
    } finally {
      if (generation === historyListRequestRef.current) setHistoryListLoading(false);
    }
  };
  const openResearch = async target => {
    if (chatBusyRef.current || clarifyBusyRef.current || researchActionRef.current) return;
    const generation = ++researchRequestRef.current;
    researchTargetRef.current = target;
    resetResearch();
    setResearchLoading(true);
    setResearchError("");
    if (!target.token) clearSharedLocation();
    try {
      const response = await fetch(target.token ? `${API}/shared/${encodeURIComponent(target.token)}` : `${API}/history/${target.id}`);
      if (!response.ok) throw new Error(response.status === 404 ? "Исследование недоступно: оно удалено или ссылка больше не действует." : "Не удалось открыть исследование. Проверьте доступ и повторите запрос.");
      const data = await response.json();
      if (generation !== researchRequestRef.current) return;
      applyResearch(data);
    } catch (error) {
      if (generation === researchRequestRef.current) {
        setResearchError(error.message || "Не удалось открыть исследование.");
      }
    } finally {
      if (generation === researchRequestRef.current) {
        researchAccessRef.current.loading = false;
        setResearchLoading(false);
      }
    }
  };
  const createResearch = async (showResearch = true) => {
    if (chatBusyRef.current || clarifyBusyRef.current || researchActionRef.current) return;
    // Кнопка «Новое исследование»: пустое исследование уже есть — открываем его,
    // а не создаём ещё одно (раньше 3 из 4 оставались без единого вопроса).
    // Вызовы после удаления и при первом входе (showResearch = false) создают как прежде.
    const reuse = showResearch && !researchError;
    if (reuse && workspaceId && !researchReadOnly && !chat.length) {
      if (showResearch) setView("ai_research");
      setTimeout(() => chatInputRef.current && chatInputRef.current.focus(), 0);
      return;
    }
    const empty = researches.find(item => item.has_messages === false && item.workspace_id !== workspaceId);
    if (reuse && empty) {
      if (showResearch) setView("ai_research");
      await openResearch({
        id: empty.workspace_id
      });
      return;
    }
    researchActionRef.current = true;
    setResearchActionBusy(true);
    const generation = ++researchRequestRef.current;
    // Повтор после сбоя создания должен снова создавать, а не открывать
    // предыдущую историю, которая пока остаётся на экране.
    researchTargetRef.current = {
      create: true,
      showResearch
    };
    setResearchLoading(true);
    setResearchError("");
    researchAccessRef.current.loading = true;
    try {
      const response = await fetch(`${API}/workspace`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          name: null
        })
      });
      if (!response.ok) throw new Error("Не удалось создать исследование. Повторите запрос.");
      const data = await response.json();
      if (!data.workspace_id) throw new Error("Не удалось создать исследование.");
      if (generation !== researchRequestRef.current) return;
      resetResearch();
      researchTargetRef.current = {
        id: data.workspace_id
      };
      clearSharedLocation();
      applyResearch({
        workspace: {
          workspace_id: data.workspace_id,
          name: "Новое исследование"
        },
        messages: [],
        reports: [],
        read_only: false
      });
      if (showResearch) setView("ai_research");
      await loadResearchList();
    } catch (error) {
      if (generation === researchRequestRef.current) {
        setResearchError(error.message || "Не удалось создать исследование.");
      }
    } finally {
      if (generation === researchRequestRef.current) {
        researchAccessRef.current.loading = false;
        setResearchLoading(false);
      }
      researchActionRef.current = false;
      setResearchActionBusy(false);
    }
  };
  const initializeResearch = async () => {
    const target = researchTargetRef.current;
    if (target.token) {
      setView("ai_research");
      loadResearchList();
      await openResearch(target);
      return;
    }
    const generation = researchRequestRef.current;
    const list = await loadResearchList();
    if (generation !== researchRequestRef.current) return;
    if (list === null) {
      researchAccessRef.current.loading = false;
      setResearchLoading(false);
      return;
    }
    if (list.length) await openResearch({
      id: list[0].workspace_id
    });else await createResearch(false);
  };
  useEffect(() => {
    if (!authz || !authz.contexts) return;
    initializeResearch();
    return () => {
      researchRequestRef.current += 1;
      historyListRequestRef.current += 1;
    };
  }, [authz]);
  const shareResearch = async () => {
    if (!workspaceId || researchAccessRef.current.readOnly || researchAccessRef.current.loading || researchActionRef.current || chatBusyRef.current || clarifyBusyRef.current) return;
    researchActionRef.current = true;
    setResearchActionBusy(true);
    try {
      const response = await fetch(`${API}/workspace/${workspaceId}/share`, {
        method: "POST"
      });
      if (!response.ok) throw new Error("Не удалось создать ссылку. Повторите запрос.");
      const data = await response.json();
      if (!data.share_url) throw new Error("Сервер не вернул ссылку на исследование.");
      const url = new URL(data.share_url, window.location.href).href;
      setResearchShareUrl(url);
      try {
        await navigator.clipboard.writeText(url);
        showToast("Ссылка скопирована. Получателю потребуется вход в модуль.", "success");
      } catch {
        showToast("Ссылка готова. Скопируйте её из поля ниже.", "info");
      }
    } catch (error) {
      showToast(error.message || "Не удалось создать ссылку.", "error");
    } finally {
      researchActionRef.current = false;
      setResearchActionBusy(false);
    }
  };
  const requestResearchDelete = research => {
    if (!research || researchAccessRef.current.loading || researchActionRef.current || chatBusyRef.current || clarifyBusyRef.current) return;
    setResearchDeleteError("");
    setResearchDeleteTarget(research);
    setResearchDeleteConfirm(true);
  };
  const deleteResearch = async () => {
    const deletedId = researchDeleteTarget && researchDeleteTarget.workspace_id;
    if (!deletedId || researchAccessRef.current.loading || researchActionRef.current || chatBusyRef.current || clarifyBusyRef.current) return;
    researchActionRef.current = true;
    setResearchActionBusy(true);
    setResearchDeleteError("");
    const deletedWasOpen = workspaceId === deletedId;
    let deleted = false;
    try {
      const response = await fetch(`${API}/workspace/${deletedId}`, {
        method: "DELETE"
      });
      if (!response.ok) throw new Error("Не удалось удалить исследование из истории. Повторите запрос.");
      deleted = true;
      // Список, запрошенный до удаления, уже устарел: его поздний ответ
      // не должен возвращать удалённую строку или менять индикатор загрузки.
      historyListRequestRef.current += 1;
      setHistoryListLoading(false);
      setHistoryListError("");
      // Убираем строку только после подтверждения сервера.
      setResearches(previous => previous.filter(item => item.workspace_id !== deletedId));
      setResearchDeleteConfirm(false);
      setResearchDeleteTarget(null);
      if (deletedWasOpen) {
        resetResearch();
        researchTargetRef.current = {};
        clearSharedLocation();
      }
      showToast("Исследование удалено из истории. Данные сохранены в системе.", "success");
    } catch (error) {
      setResearchDeleteError(error.message || "Не удалось удалить исследование.");
    } finally {
      researchActionRef.current = false;
      setResearchActionBusy(false);
    }
    if (deleted && deletedWasOpen) {
      const next = researches.find(item => item.workspace_id !== deletedId);
      if (next) await openResearch({
        id: next.workspace_id
      });else await createResearch(false);
    }
  };

  // Таймер toast очищается при размонтировании (нет setState после unmount).
  useEffect(() => () => {
    clearTimeout(toastTimerRef.current);
    if (csvUrlRef.current) URL.revokeObjectURL(csvUrlRef.current);
  }, []);

  // ── Ручная маркировка: POST /records/verdict + toast результата ──────────
  const markVerdict = async (ids, classification, comment, {
    quiet = false,
    source = null
  } = {}) => {
    if (!canMarkVerdict || !ids.length || markBusy) return false;
    setMarkBusy(true);
    try {
      const r = await fetch(`${API}/records/verdict`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          record_ids: ids,
          classification,
          comment: comment || null,
          source
        })
      });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        if (r.status === 401 || r.status === 403) {
          setAuthz(prev => prev && prev.contexts ? {
            ...prev,
            capabilities: {
              ...prev.capabilities,
              can_mark_verdict: false
            }
          } : prev);
          setVerdictModal(null);
          setMarkComment("");
        }
        showToast(d && typeof d.detail === "string" && d.detail || "Ошибка маркировки.", "error");
        return false;
      }
      if (d && d.skipped && d.skipped.length) {
        showToast(`Пропущено записей: ${d.skipped.length} (не найдены).`, "info");
      } else if (!quiet) {
        showToast("Вердикт сохранён.", "success");
      }
      // Строки базы обновляем на месте — запись не пропадает из-под курсора;
      // карточку перечитываем, чтобы в истории появилось решение.
      const decided = new Set(ids);
      setRecords(prev => prev.map(r => decided.has(r.record_id) ? {
        ...r,
        classification,
        is_loophole: classification !== "not_confirmed",
        verdict_model: "manual",
        verdict_confidence: 1,
        reviewed: true,
        awaiting: false
      } : r));
      setContentCache(prev => {
        const next = {
          ...prev
        };
        ids.forEach(id => {
          delete next[id];
        });
        return next;
      });
      loadSummary();
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
      const r = await fetch(`${API}/queue?sort=${queueSortRef.current}`);
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
      setQueueTotal(Number.isInteger(d.total) ? d.total : nextRecords.length);
      setQueueSelectedId(prev => nextRecords.some(r => r.record_id === prev) ? prev : nextRecords[0] ? nextRecords[0].record_id : null);
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
  const openContext = id => {
    if (id === "queue") {
      // Маршрут переключаем синхронно при клике: поздний ответ /queue
      // не вырывает вид обратно в очередь, если пользователь уже ушёл
      // в другой контекст (race-фикс ревью 1.4).
      setView("queue");
      loadQueue();
      return;
    }
    if (id === "admin") {
      // Администрирование — панель «Доступ» поверх текущего раздела.
      setAccessOpen(true);
      loadAdmin();
      return;
    }
    // Маршрут = контекст: каталог и AI-исследование не делят рабочую поверхность.
    setView(id);
  };
  const onContextTabKeyDown = event => {
    const tablist = event.currentTarget.closest('[role="tablist"]');
    if (!tablist) return;
    const tabs = [...tablist.querySelectorAll('[role="tab"]')];
    const currentIndex = tabs.indexOf(event.currentTarget);
    if (currentIndex < 0 || tabs.length === 0) return;
    let nextIndex = null;
    if (event.key === "Home") nextIndex = 0;else if (event.key === "End") nextIndex = tabs.length - 1;else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
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
      const [rRoles, rAudit] = await Promise.all([fetch(`${API}/admin/roles`), fetch(`${API}/admin/audit`)]);
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
      const [dRoles, dAudit] = await Promise.all([rRoles.json(), rAudit.json()]);
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
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          username
        })
      });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        showToast(d && typeof d.detail === "string" && d.detail || "Не удалось назначить роль.", "error");
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
  const revokeRole = async username => {
    if (adminBusy) return;
    setAdminBusy(true);
    try {
      const r = await fetch(`${API}/admin/roles/revoke`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          username
        })
      });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        showToast(d && typeof d.detail === "string" && d.detail || "Не удалось отозвать роль.", "error");
        return;
      }
      showToast(`Роль эксперта ЦК КС отозвана: ${username}.`, "success");
      await loadAdmin();
    } catch (e) {
      showToast("Не удалось отозвать роль: " + String(e), "error");
    } finally {
      setAdminBusy(false);
    }
  };

  // ── Активные слои: focus-trap, Escape, возврат фокуса (story 1.4) ─────────
  useFocusLayer(!!verdictModal, verdictDialogRef, () => setVerdictModal(null));
  // Деструктивное действие: начальный фокус — «Отмена», а не «Удалить».
  useFocusLayer(!!deleteConfirm, confirmDialogRef, () => setDeleteConfirm(null), confirmCancelRef, sourcesTabRef);
  useFocusLayer(researchDeleteConfirm, researchDeleteDialogRef, () => {
    if (!researchActionRef.current) {
      setResearchDeleteConfirm(false);
      setResearchDeleteTarget(null);
    }
  }, researchDeleteCancelRef, researchTabRef);

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
    if (!url || !description || !workspaceId || researchAccessRef.current.readOnly || researchAccessRef.current.loading) return;
    if (!WEB_TARGET_RE.test(url)) {
      setParserError("Укажите полный URL веб-источника, начиная с http:// или https://");
      return;
    }
    setParsersBusy(true);
    setParserError("");
    try {
      const r = await fetch(`${API}/parser-requests`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          workspace_id: workspaceId,
          url,
          description
        })
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
    setLogPanel({
      parserId,
      runId,
      lines: [],
      done: null,
      error: null
    });
    closeLogEs(); // закрываем предыдущее соединение, чтобы не плодить утечки
    const es = new EventSource(`${API}/parsers/${parserId}/log/stream?run_id=${runId}`);
    let terminal = false;
    logEsRef.current = es;
    es.addEventListener("log", e => {
      setLogPanel(prev => prev && prev.runId === runId ? {
        ...prev,
        lines: [...prev.lines, e.data]
      } : prev);
    });
    es.addEventListener("done", e => {
      terminal = true;
      es.close();
      if (logEsRef.current === es) logEsRef.current = null;
      let payload = null;
      try {
        payload = JSON.parse(e.data);
      } catch {}
      setLogPanel(prev => prev && prev.runId === runId ? {
        ...prev,
        done: payload || {
          status: "завершено"
        },
        error: null
      } : prev);
      loadParsers();
    });
    es.onerror = () => {
      if (terminal) return;
      terminal = true;
      es.close();
      if (logEsRef.current === es) logEsRef.current = null;
      setLogPanel(prev => prev && prev.runId === runId && !prev.done ? {
        ...prev,
        error: "Соединение с журналом прервано. Повторите запуск или обновите список."
      } : prev);
    };
  };
  const startParser = async pid => {
    setParsersBusy(true);
    try {
      const r = await fetch(`${API}/parsers/${pid}/run`, {
        method: "POST"
      });
      const d = await r.json().catch(() => null);
      if (!r.ok || !d || !d.run_id) {
        throw new Error(d && typeof d.detail === "string" ? d.detail : "Запуск невозможен");
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
  const healParser = async pid => {
    setParsersBusy(true);
    try {
      const r = await fetch(`${API}/parsers/${pid}/heal`, {
        method: "POST"
      });
      const d = await r.json().catch(() => null);
      if (!r.ok || !d || !d.heal_run_id) {
        throw new Error(d && typeof d.detail === "string" ? d.detail : "Восстановление недоступно");
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
  const openEdit = p => {
    setEditParserId(p.parser_id);
    setEditForm({
      name: p.name || "",
      cron_expr: p.cron_expr || "",
      auto_enabled: !!p.auto_enabled
    });
    setEditError("");
  };
  const saveEdit = async () => {
    setParsersBusy(true);
    setEditError("");
    try {
      const r = await fetch(`${API}/parsers/${editParserId}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          name: editForm.name,
          cron_expr: editForm.cron_expr,
          // "" очищает расписание (бэкенд → NULL)
          auto_enabled: editForm.auto_enabled
        })
      });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        throw new Error(d && typeof d.detail === "string" ? d.detail : `Ошибка сохранения (HTTP ${r.status})`);
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
      const r = await fetch(`${API}/parsers/${p.parser_id}`, {
        method: "DELETE"
      });
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
  const stopParser = async pid => {
    setParsersBusy(true);
    try {
      const r = await fetch(`${API}/parsers/${pid}/stop`, {
        method: "POST"
      });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        throw new Error(d && typeof d.detail === "string" ? d.detail : "Остановка невозможна");
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
    if (!userMsg || !userMsg.trim() || !workspaceId || researchAccessRef.current.readOnly || researchAccessRef.current.loading || researchActionRef.current || chatBusyRef.current || !skipClarify && clarifyBusyRef.current) return false;
    chatBusyRef.current = true;
    const researchGeneration = researchRequestRef.current;
    setResearchActivity(null);
    // Token одноразовый: новый challenge принимаем только из server-side SSE.
    setClarificationToken(null);
    // запоминаем ИСХОДНЫЙ запрос (не enriched) — из него build_enriched_question
    // соберёт обогащённый вопрос после ответов на уточнения
    if (!skipClarify) {
      setPendingQuery(userMsg);
      setPhase(null);
      setSubtasks([]);
      setChat(prev => [...prev, {
        role: "user",
        content: userMsg
      }]);
    }
    if (overrideMessage == null) setChatInput("");
    setChatLoading(true);
    setClarifyError("");
    setToolEvents([]);
    setSubagents([]);
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
                _clarificationQuestion: marker
              });
            }
          });
          return copy;
        });
      }
    };
    try {
      const resp = await fetch(`${API}/chat`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          workspace_id: workspaceId,
          message: userMsg,
          history: chat,
          clarify_token: serverClarificationToken || null
        })
      });
      if (researchGeneration !== researchRequestRef.current) return false;
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
              ...(reportId ? {
                report_id: reportId
              } : {})
            };
          } else {
            copy.push({
              role: "assistant",
              content: finalText,
              _live: false,
              ...(reportId ? {
                report_id: reportId
              } : {})
            });
          }
          return copy;
        });
        gotAnyToken = false;
        assistantMsg = "";
      };
      while (true) {
        const {
          done,
          value
        } = await reader.read();
        if (researchGeneration !== researchRequestRef.current) {
          await reader.cancel();
          return false;
        }
        if (done) break;
        buf += decoder.decode(value, {
          stream: true
        });
        const lines = buf.split("\n");
        buf = lines.pop();
        for (const line of lines) {
          if (!line) continue;
          if (line.startsWith("event:")) {
            sseEventType = line.slice(6).trim();
          } else if (line.startsWith("data:")) {
            const raw = line.slice(5).trim();
            let payload = null;
            try {
              payload = JSON.parse(raw);
            } catch {
              payload = raw;
            }
            switch (sseEventType) {
              case "token":
                {
                  const piece = typeof payload === "string" ? payload : payload && payload.text || "";
                  assistantMsg += piece;
                  gotAnyToken = true;
                  setChat(prev => {
                    const copy = [...prev];
                    if (copy.length && copy[copy.length - 1].role === "assistant" && copy[copy.length - 1]._live) {
                      copy[copy.length - 1] = {
                        ...copy[copy.length - 1],
                        content: assistantMsg
                      };
                    } else {
                      copy.push({
                        role: "assistant",
                        content: assistantMsg,
                        _live: true
                      });
                    }
                    return copy;
                  });
                  break;
                }
              case "partial":
                {
                  const message = payload && payload.message;
                  if (typeof message !== "string" || !message) break;
                  assistantMsg += (assistantMsg ? "\n\n" : "") + message;
                  gotAnyToken = true;
                  setChat(prev => {
                    const copy = [...prev];
                    if (copy.length && copy[copy.length - 1].role === "assistant" && copy[copy.length - 1]._live) {
                      copy[copy.length - 1] = {
                        ...copy[copy.length - 1],
                        content: assistantMsg
                      };
                    } else {
                      copy.push({
                        role: "assistant",
                        content: assistantMsg,
                        _live: true
                      });
                    }
                    return copy;
                  });
                  break;
                }
              case "phase":
                {
                  const p = payload && payload.phase || payload;
                  if (typeof p === "string") {
                    setPhase(p);
                    const hasActivity = p === "execute" && payload && ["waiting_model", "research_tools"].includes(payload.stage) && typeof payload.message === "string" && payload.message.trim() && Number.isInteger(payload.elapsed_seconds) && payload.elapsed_seconds >= 0;
                    setResearchActivity(hasActivity ? {
                      message: payload.message,
                      elapsed: payload.elapsed_seconds
                    } : null);
                    if (p === "error") {
                      terminalError = true;
                      terminalErrorMessage = publicChatErrorMessage(payload && typeof payload.message === "string" ? payload.message : "Исследование не запустилось.");
                    }
                  }
                  break;
                }
              case "question":
                {
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
              case "subtask":
                {
                  const title = payload && payload.title || "";
                  const status = payload && payload.status || "running";
                  if (!title) break;
                  setSubtasks(prev => {
                    const idx = prev.findIndex(s => s.title === title);
                    if (idx >= 0) {
                      const copy = [...prev];
                      copy[idx] = {
                        ...copy[idx],
                        status
                      };
                      return copy;
                    }
                    return [...prev, {
                      title,
                      status
                    }];
                  });
                  break;
                }
              case "subagent":
                {
                  const event = acceptSubagentEvent(payload);
                  if (!event) break;
                  setSubagents(prev => {
                    const index = prev.findIndex(agent => agent.id === event.id);
                    if (index < 0) return [...prev, event].slice(0, 18);
                    return prev.map((agent, i) => i === index ? event : agent);
                  });
                  break;
                }
              case "records":
                {
                  // Записи, которые агент просмотрел, не подменяют список «Базы»:
                  // находки исследования приходят отдельным блоком после итога.
                  break;
                }
              case "tool_call":
              case "tool_result":
                {
                  const name = payload && payload.name || "tool";
                  const event = {
                    kind: sseEventType === "tool_call" ? "call" : "result",
                    name,
                    status: payload.status === "failed" ? "failed" : "completed",
                    ts: Date.now()
                  };
                  setToolEvents(prev => [...prev, event]);
                  setChat(prev => {
                    const copy = [...prev];
                    const last = copy[copy.length - 1];
                    if (last && last.role === "assistant" && last._live) {
                      copy[copy.length - 1] = {
                        ...last,
                        tools: [...(last.tools || []), event]
                      };
                    } else copy.push({
                      role: "assistant",
                      content: "",
                      _live: true,
                      tools: [event]
                    });
                    return copy;
                  });
                  break;
                }
              case "answer":
              case "done":
                {
                  setResearchActivity(null);
                  // финализация — закрываем "живое" сообщение ассистента
                  flushAssistant();
                  if (sseEventType === "done" && !terminalError) {
                    setPhase("done");
                  }
                  break;
                }
              case "report":
                {
                  if (payload && Number.isInteger(payload.report_id) && payload.report_id > 0) {
                    reportId = payload.report_id;
                    setChat(prev => {
                      const copy = [...prev];
                      for (let index = copy.length - 1; index >= 0; index -= 1) {
                        if (copy[index].role === "assistant" && !copy[index]._clarificationQuestion) {
                          copy[index] = {
                            ...copy[index],
                            report_id: reportId
                          };
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
          content: `Ошибка: ${terminalErrorMessage}`
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
            return [...prev, {
              role: "assistant",
              content: "(пустой ответ)"
            }];
          }
          return prev;
        });
      }
      return true;
    } catch (e) {
      if (researchGeneration !== researchRequestRef.current) return false;
      const message = publicChatErrorMessage(e instanceof Error && e.message ? e.message : String(e));
      setPhase("error");
      if (!skipClarify) setChatInput(userMsg);
      setChat(prev => [...prev, {
        role: "assistant",
        content: "Ошибка: " + message
      }]);
      return false;
    } finally {
      chatBusyRef.current = false;
      if (researchGeneration === researchRequestRef.current) {
        setResearchActivity(null);
        setChat(prev => prev.map(message => message._live ? {
          ...message,
          _live: false
        } : message));
        setSubagents(prev => prev.map(agent => ["queued", "searching", "classifying"].includes(agent.status) ? {
          ...agent,
          status: "cancelled"
        } : agent));
        setChatLoading(false);
        loadResearchList();
      }
      // Подтягиваем в таблицу подтверждённые находки, сохранённые серверным
      // этапом после завершения managed-запуска.
      loadRecords();
    }
  }, [chatInput, workspaceId, chat, loadRecords]);

  // ── Уточняющие вопросы: helpers ──────────────────────────────────────────
  const toggleAnswer = (qid, value, multi) => {
    setClarifyError("");
    setAnswersByQ(prev => {
      const cur = prev[qid] || {
        selected: [],
        other: ""
      };
      const sel = cur.selected;
      if (multi) {
        const has = sel.includes(value);
        return {
          ...prev,
          [qid]: {
            ...cur,
            selected: has ? sel.filter(x => x !== value) : [...sel, value]
          }
        };
      }
      return {
        ...prev,
        [qid]: {
          ...cur,
          selected: [value]
        }
      };
    });
  };
  const setOtherText = (qid, text) => {
    setClarifyError("");
    setAnswersByQ(prev => ({
      ...prev,
      [qid]: {
        ...(prev[qid] || {
          selected: [],
          other: ""
        }),
        other: text
      }
    }));
  };
  const submitAnswers = async () => {
    if (!pendingQuestions || !pendingQuestions.length || !clarificationToken || clarifySubmitting || clarifyBusyRef.current || chatBusyRef.current || researchActionRef.current || researchAccessRef.current.readOnly || researchAccessRef.current.loading) return;
    const q = pendingQuestions[0];
    const questionsForRetry = pendingQuestions;
    const clarificationTokenForRetry = clarificationToken;
    const answersForRetry = answersByQ;
    const inputForRetry = chatInput;
    const answersPayload = pendingQuestions.map(pq => {
      const a = pq.type === "text" ? {
        selected: [],
        other: chatInput.trim()
      } : answersByQ[pq.id] || {
        selected: [],
        other: ""
      };
      return {
        question: pq.question,
        selected: (a.selected || []).filter(Boolean),
        other: (a.other || "").trim()
      };
    });
    const missingAnswer = answersPayload.some(a => !a.selected.length && !a.other);
    if (missingAnswer) {
      setClarifyError("Ответьте на уточняющий вопрос перед запуском исследования.");
      return;
    }
    const optimisticContent = answersPayload.map(a => [...a.selected, a.other].filter(Boolean).join(", ")).filter(Boolean).join("; ");
    const optimisticId = `clarify-answer-${Date.now()}-${Math.random()}`;
    clarifyBusyRef.current = true;
    setClarifySubmitting(true);
    setClarifyError("");
    setChat(prev => [...prev, {
      role: "user",
      content: optimisticContent,
      _clarificationAnswer: optimisticId
    }]);
    setChatInput("");
    // Optimistic-state: ответ виден сразу, controls скрыты, busy остаётся
    // непрерывным до окончания следующего /chat с execution token.
    setPendingQuestions(null);
    setClarificationToken(null);
    setAnswersByQ({});
    try {
      const r = await fetch(`${API}/clarify/answer`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        // ИСХОДНЫЙ запрос пользователя (pendingQuery), НЕ текст уточняющего
        // вопроса — иначе enriched строится из вопроса и агент ищет ерунду
        body: JSON.stringify({
          workspace_id: workspaceId,
          question: pendingQuery || q.question,
          answers: answersPayload,
          clarification_token: clarificationToken
        })
      });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        const detail = d && d.detail;
        const message = typeof detail === "string" ? detail : detail && detail.message || "Не удалось отправить ответ.";
        const requestError = new Error(message);
        requestError.status = r.status;
        throw requestError;
      }
      const enriched = d && d.enriched_question || (typeof d === "string" ? d : "");
      const executionToken = d && d.execution_token;
      if (enriched && executionToken) {
        const confirmedContent = d && typeof d.answer_message === "string" ? d.answer_message : optimisticContent;
        setChat(prev => prev.map(message => message._clarificationAnswer === optimisticId ? {
          ...message,
          content: confirmedContent
        } : message));
        // Только server-side execution token разрешает продолжить после clarify.
        const chatStarted = await sendChat(enriched, {
          clarificationToken: executionToken
        });
        if (!chatStarted) {
          setChatInput(enriched);
          setClarifyError("Ответ на уточнение сохранён, но исследование не запустилось. " + "Подготовленный запрос оставлен в поле — отправьте его ещё раз.");
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
        setChatInput(`${originalQuery}\n\nОтвет на уточнение: ${optimisticContent}`.trim());
        setPhase("error");
        setClarifyError("Уточнение истекло или уже использовано. " + "Исходный запрос и ответ оставлены в поле — отправьте их заново.");
        return;
      }
      setPendingQuestions(questionsForRetry);
      setClarificationToken(clarificationTokenForRetry);
      setAnswersByQ(answersForRetry);
      setChatInput(inputForRetry);
      setClarifyError(e instanceof Error && e.message ? e.message : "Не удалось отправить ответ.");
    } finally {
      clarifyBusyRef.current = false;
      setClarifySubmitting(false);
    }
  };

  // Автоскролл чата вниз.
  useEffect(() => {
    if (chatScrollRef.current) {
      chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight;
    }
  }, [chat, chatLoading, pendingQuestions, subtasks, toolEvents]);
  const fmtDate = v => {
    if (!v) return "—";
    const date = new Date(v);
    if (Number.isNaN(date.getTime())) return "—";
    // Дата без времени (или ровно полночь) — только число: «00:00» — вымышленная точность.
    const hasTime = /[T ]\d{2}:\d{2}/.test(String(v)) && (date.getHours() || date.getMinutes());
    return hasTime ? date.toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit"
    }) : date.toLocaleDateString("ru-RU");
  };
  const researchListName = name => {
    const value = String(name || "Без названия").trim() || "Без названия";
    return value.length <= 64 ? value : `${value.slice(0, 63)}…`;
  };
  const queueSelected = queueRecords.find(r => r.record_id === queueSelectedId) || queueRecords[0] || null;
  const phasePosition = phase ? PHASES.indexOf(phase) : -1;
  const researchProgress = phase === "done" ? 100 : phasePosition >= 0 ? Math.round((phasePosition + 1) / PHASES.length * 100) : 0;
  // Метки решений ЦК КС (loophole_verification_decision.decision) для карточки
  // очереди и модалки вердикта.
  const decisionLabel = value => ({
    vulnerability: "Уязвимость",
    fraud_scheme: "Мошенническая схема",
    not_confirmed: "Не подтверждено"
  })[value] || value;
  const downloadResearchReport = async (reportId, format) => {
    if (!reportId || researchAccessRef.current.readOnly || researchAccessRef.current.loading) return;
    try {
      const r = await fetch(`${API}/research/reports/${reportId}/export/${format}`);
      if (!r.ok) {
        const payload = await r.json().catch(() => null);
        const detail = payload && payload.detail;
        throw new Error(detail && detail.message || "Не удалось скачать исследование.");
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      triggerCsvDownload({
        url,
        filename: `research-report-${reportId}.${format === "docx" ? "docx" : "pdf"}`
      });
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Не удалось скачать исследование.", "error");
    }
  };

  // Ленивая загрузка полного контента записи в кэш (без повторных запросов).
  const loadContent = id => {
    if (contentCache[id]) return;
    setContentCache(prev => ({
      ...prev,
      [id]: {
        loading: true,
        data: null,
        error: null
      }
    }));
    fetch(`${API}/records/${id}/content`).then(r => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))).then(data => setContentCache(prev => ({
      ...prev,
      [id]: {
        loading: false,
        data,
        error: null
      }
    }))).catch(e => setContentCache(prev => ({
      ...prev,
      [id]: {
        loading: false,
        data: null,
        error: String(e)
      }
    })));
  };

  // Карточка очереди: при смене выбранной записи сбрасываем комментарий
  // участника ЦК (поле карточки и поле модалки вердикта — одно состояние
  // markComment) и лениво догружаем полный текст записи; contentCache
  // исключает повторные сетевые запросы при возврате к записи. Состояние
  // expanded (раскрытые детали каталога) здесь не трогаем: таблица каталога
  // не должна раскрываться из-за просмотра карточки очереди.
  const queueSelectedRecordId = queueSelected ? queueSelected.record_id : null;
  useEffect(() => {
    setMarkComment("");
    if (queueSelectedRecordId) loadContent(queueSelectedRecordId);
  }, [queueSelectedRecordId]);
  const currentQuestions = pendingQuestions || [];
  const pendingTextQuestion = currentQuestions.find(q => q && q.type === "text") || null;
  const selectionQuestions = currentQuestions.filter(q => q && q.type !== "text");
  const textClarification = !!pendingTextQuestion && selectionQuestions.length === 0;
  const selectionAnswersComplete = selectionQuestions.length > 0 && selectionQuestions.every(q => {
    const answer = answersByQ[q.id] || {
      selected: [],
      other: ""
    };
    return answer.selected.length > 0 || !!answer.other.trim();
  });
  const agentBusy = clarifySubmitting || chatLoading;

  // ══ Вкладка «Уязвимости» в системе AuditLens (макет, согласованный 27.09) ══
  // База — сводка, фильтры, список и карточка записи; «Исследовать» — одна
  // колонка с ходом работы и находками; «Очередь» — решение на карточке;
  // «Доступ» — панель администратора. Права по-прежнему решает сервер.

  // ── Сводка над базой и счётчики фильтров ──────────────────────────────────
  const [summaryData, setSummaryData] = useState(null);
  const summaryRequestRef = useRef(0);
  const summaryTextRef = useRef("");
  const loadSummary = useCallback(async () => {
    const generation = ++summaryRequestRef.current;
    const params = new URLSearchParams();
    if (fText.trim()) params.set("q", fText.trim());
    if (fBanks.length) params.set("bank_slugs", fBanks.join(","));
    if (fFrom) params.set("period_from", fFrom);
    if (fTo) params.set("period_to", fTo);
    params.set("verification_status", fVerification);
    params.set("classification", fClassification);
    try {
      const r = await fetch(`${API}/catalog/summary?${params.toString()}`);
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      if (generation === summaryRequestRef.current) setSummaryData(d);
    } catch {
      if (generation === summaryRequestRef.current) setSummaryData(null);
    }
  }, [fText, fBanks, fFrom, fTo, fVerification, fClassification]);
  useEffect(() => {
    if (!authz || !authz.contexts) return undefined;
    const typing = summaryTextRef.current !== fText;
    summaryTextRef.current = fText;
    const timer = setTimeout(() => loadSummary(), typing ? 350 : 0);
    return () => clearTimeout(timer);
  }, [loadSummary, authz]);

  // ── Состояние интерфейса ───────────────────────────────────────────────────
  const [fPeriod, setFPeriod] = useState("all"); // all | 7 | 30 | 90 | custom
  const [selId, setSelId] = useState(null);
  // Запись, открытая из исследования: её карточка видна, даже если её нет в
  // текущей выборке; смена фильтров это снимает.
  const [pinnedId, setPinnedId] = useState(null);
  const qCardRef = useRef(null);
  const [readerOpen, setReaderOpen] = useState(false);
  const [selectMode, setSelectMode] = useState(false);
  const [showFilters, setShowFilters] = useState(false);
  const [pop, setPop] = useState(null); // {kind, rect, record?}
  const [summaries, setSummaries] = useState({}); // record_id → {loading, text, reason}
  const [fullOpen, setFullOpen] = useState(new Set());
  const [cases, setCases] = useState(null);
  const [newCaseTitle, setNewCaseTitle] = useState("");
  const [qSort, setQSort] = useState("old");
  const [qOpen, setQOpen] = useState(false);
  const [drafts, setDrafts] = useState({}); // record_id → {cls, comment}
  const [reviewedCount, setReviewedCount] = useState(0);
  const [hiddenQueueIds, setHiddenQueueIds] = useState(new Set());
  const pendingRef = useRef(null); // отложенное решение ЦК КС
  const [accessOpen, setAccessOpen] = useState(false);
  const [hiddenExperts, setHiddenExperts] = useState(new Set());
  const revokeTimersRef = useRef({});
  const [histOpen, setHistOpen] = useState(false);
  const [agentsOpen, setAgentsOpen] = useState(true); // подробности по исследователям
  const accessSheetRef = useRef(null);
  const searchRef = useRef(null);
  const commentRef = useRef(null);
  const readerRef = useRef(null);
  const hasContext = id => !!(authz && authz.contexts && authz.contexts.some(c => c.id === id));
  const canQueue = hasContext("queue");
  const canAdmin = hasContext("admin");
  const totals = summaryData && summaryData.totals;
  const facets = summaryData && summaryData.facets;

  // Период — пресеты по дате публикации (как фильтр «Дата публикации» раньше).
  const applyPeriod = value => {
    setFPeriod(value);
    if (value === "all") {
      setFFrom("");
      setFTo("");
      return;
    }
    if (value === "custom") return;
    const from = new Date(Date.now() - Number(value) * 86400000);
    setFFrom(from.toISOString().slice(0, 10));
    setFTo("");
  };
  const resetAll = () => {
    resetFilters();
    setFPeriod("all");
    setFSort("new");
  };
  const activeFilterCount = (fClassification !== "confirmed" ? 1 : 0) + fBanks.length + (fFrom || fTo ? 1 : 0) + (fVerification !== "all" ? 1 : 0);

  // ── Выбранная запись и карточка ────────────────────────────────────────────
  const detailOf = id => {
    const entry = id != null ? contentCache[id] : null;
    return entry && entry.data ? entry.data : null;
  };
  const selRecord = selId != null && (records.find(r => r.record_id === selId) || (pinnedId === selId && detailOf(selId) ? {
    record_id: selId,
    ...detailOf(selId)
  } : null)) || records[0] || null;
  const selRecordId = selRecord ? selRecord.record_id : null;

  // Карточка перечитывается и после смены вердикта: markVerdict убирает её из кэша.
  const selCached = selRecordId != null && !!contentCache[selRecordId];
  useEffect(() => {
    if (view === "catalog" && selRecordId != null && !selCached) loadContent(selRecordId);
  }, [view, selRecordId, selCached]); // eslint-disable-line react-hooks/exhaustive-deps

  // Находки исследования: после итога перечитываем, что попало в общую базу.
  useEffect(() => {
    if (chatLoading || !workspaceId || phase !== "done" || researchReadOnly) return;
    fetch(`${API}/research/workspace/${workspaceId}/findings`).then(r => r.ok ? r.json() : null).then(d => {
      if (d && Array.isArray(d.findings)) setFindings(d.findings);
    }).catch(() => {});
  }, [phase, chatLoading, workspaceId, researchReadOnly]);

  // Суть — только уязвимостям и схемам, один вызов модели на запись (сервер
  // сохраняет результат). «Не подтверждено» модель не трогает.
  const ensureSummary = record => {
    if (!record) return;
    const id = record.record_id;
    const detail = detailOf(id);
    const kind = recordKind({
      ...record,
      ...(detail || {})
    });
    if (!POSITIVE_KINDS.has(kind)) return;
    if (detail && detail.summary || record.summary || summaries[id]) return;
    setSummaries(prev => ({
      ...prev,
      [id]: {
        loading: true
      }
    }));
    fetch(`${API}/records/${id}/summary`, {
      method: "POST"
    }).then(r => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))).then(d => setSummaries(prev => ({
      ...prev,
      [id]: {
        loading: false,
        text: d.summary,
        headline: d.headline,
        doubt: d.doubt,
        reason: d.reason
      }
    }))).catch(() => setSummaries(prev => ({
      ...prev,
      [id]: {
        loading: false,
        text: null,
        reason: "llm_error"
      }
    })));
  };
  const currentReaderRecord = view === "queue" ? queueRecords.find(r => r.record_id === queueSelectedId) || null : selRecord;
  const currentReaderDetail = currentReaderRecord ? detailOf(currentReaderRecord.record_id) : null;
  useEffect(() => {
    if (currentReaderRecord && currentReaderDetail) ensureSummary(currentReaderRecord);
  }, [currentReaderRecord && currentReaderRecord.record_id, !!currentReaderDetail]); // eslint-disable-line react-hooks/exhaustive-deps

  // Новая выборка на узком экране показывает список, а не карточку прежней записи.
  // Новая выборка открывает свою первую запись, а не карточку прежней.
  useEffect(() => {
    setReaderOpen(false);
    setSelId(null);
    setPinnedId(null);
  }, [fText, fBanks, fFrom, fTo, fVerification, fClassification, fSort]);

  // Узкий экран: открытая запись — к началу карточки (под липкими вкладками),
  // фокус на её заголовок, чтобы экранное чтение продолжилось с него.
  const showReader = node => {
    if (window.innerWidth >= 900) return;
    setTimeout(() => {
      const target = node && node.current;
      if (!target) return;
      const top = target.getBoundingClientRect().top + window.scrollY - 64;
      window.scrollTo({
        top: Math.max(0, top)
      });
      const title = target.querySelector(".lp-rd-title");
      if (title) title.focus({
        preventScroll: true
      });
    }, 0);
  };
  const pickRecord = id => {
    setSelId(id);
    setReaderOpen(true);
    if (readerRef.current) readerRef.current.scrollTop = 0;
    showReader(readerRef);
  };
  const moveRecord = delta => {
    if (!records.length) return;
    const i = Math.max(0, records.findIndex(r => r.record_id === selRecordId));
    const next = records[Math.max(0, Math.min(records.length - 1, i + delta))];
    if (next && next.record_id !== selRecordId) {
      setSelId(next.record_id);
      if (readerRef.current) readerRef.current.scrollTop = 0;
      const node = document.getElementById(`lp-item-${next.record_id}`);
      if (node) node.scrollIntoView({
        block: "nearest"
      });
    }
  };
  const openRecordInBase = record => {
    setView("catalog");
    setSelId(record.record_id);
    setPinnedId(record.record_id);
    setReaderOpen(true);
    loadContent(record.record_id);
    window.scrollTo({
      top: 0
    });
  };
  const deeperResearch = record => {
    if (agentBusy) {
      showToast("Дождитесь итога текущего исследования.", "info");
      return;
    }
    setChatInput(`Разбери подробнее: «${record.title || record.snippet || "запись"}». ` + "Как устроен механизм, у каких ещё банков встречается и как проверить в данных.");
    setView("ai_research");
    setTimeout(() => chatInputRef.current && chatInputRef.current.focus(), 50);
  };

  // ── Выгрузка в Excel в стиле AuditLens ────────────────────────────────────
  const exportExcel = async () => {
    const ids = selectMode ? [...selected] : [];
    const body = ids.length ? {
      record_ids: ids
    } : {
      bank_slugs: fBanks,
      period_from: fFrom || null,
      period_to: fTo || null,
      q: fText.trim() || null,
      verification_status: fVerification,
      classification: fClassification,
      sort: fSort
    };
    try {
      const r = await fetch(`${API}/export/catalog.xlsx`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify(body)
      });
      if (!r.ok) {
        const d = await r.json().catch(() => null);
        showToast(d && typeof d.detail === "string" && d.detail || "Не удалось сформировать Excel.", "error");
        return;
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      if (csvUrlRef.current) URL.revokeObjectURL(csvUrlRef.current);
      csvUrlRef.current = url;
      const disposition = r.headers.get("Content-Disposition") || "";
      const match = /filename="?([^";]+)"?/.exec(disposition);
      const download = {
        url,
        filename: match ? match[1] : "AuditLens_uyazvimosti.xlsx"
      };
      setLastCsvDownload(download);
      triggerCsvDownload(download);
      const n = ids.length || recordsTotal;
      showToast(`Excel сформирован · ${fmtInt(n)} ${recordWord(n)}`, "success");
    } catch (e) {
      showToast("Не удалось сформировать Excel: " + String(e), "error");
    }
  };

  // ── Аудит-дела основного приложения ───────────────────────────────────────
  const openCasePop = async (event, record) => {
    const rect = event.currentTarget.getBoundingClientRect();
    setNewCaseTitle("");
    setPop(prev => prev && prev.kind === "case" && prev.record === record ? null : {
      kind: "case",
      rect,
      record
    });
    if (cases === null) {
      try {
        const r = await fetch("/api/cases");
        const d = await r.json();
        setCases(Array.isArray(d.cases) ? d.cases : []);
      } catch {
        setCases([]);
      }
    }
  };
  const caseItemFor = record => {
    const detail = detailOf(record.record_id) || {};
    const kind = recordKind({
      ...record,
      ...detail
    });
    const bits = [KIND_LABELS[kind][0], bankName(record.bank_slug) !== "—" ? bankName(record.bank_slug) : null, detail.summary || record.summary || record.verdict_reason].filter(Boolean);
    return {
      kind: "document",
      url: record.url || null,
      title: (record.headline || record.title || record.snippet || "Запись «Уязвимостей»").slice(0, 300),
      note: bits.join(" · ").slice(0, 900)
    };
  };
  const addToCase = async (caseRow, record) => {
    setPop(null);
    try {
      const r = await fetch(`/api/cases/${caseRow.case_id}/items`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify(caseItemFor(record))
      });
      if (!r.ok) throw new Error();
      setCases(prev => (prev || []).map(c => c.case_id === caseRow.case_id ? {
        ...c,
        items: (c.items || 0) + 1
      } : c));
      showToast(`Добавлено в дело «${caseRow.title}»`, "success");
    } catch {
      showToast("Не удалось добавить в дело. Проверьте доступ к делу.", "error");
    }
  };
  const createCaseWith = async record => {
    const title = newCaseTitle.trim();
    if (!title) return;
    try {
      const r = await fetch("/api/cases", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          title
        })
      });
      const d = await r.json();
      if (!r.ok || !d.case_id) throw new Error();
      const row = {
        case_id: d.case_id,
        title,
        items: 0
      };
      setCases(prev => [row, ...(prev || [])]);
      await addToCase(row, record);
    } catch {
      showToast("Не удалось создать дело.", "error");
    }
  };

  // ── Очередь: решение на карточке с отменой в течение 10 секунд ────────────
  // Порядок задаёт сервер (qSort уходит параметром); здесь только прячем
  // записи с решением, которое ещё можно отменить.
  const queueList = useMemo(() => queueRecords.filter(r => !hiddenQueueIds.has(r.record_id)), [queueRecords, hiddenQueueIds]);
  const hiddenInQueue = queueRecords.length - queueList.length;
  const queueCount = Math.max(queueList.length, queueTotal - hiddenInQueue);
  const pickQueueSort = value => {
    if (value === qSort) return;
    setQSort(value);
    queueSortRef.current = value;
    setQueueSelectedId(null);
    loadQueue();
  };
  const qSel = queueList.find(r => r.record_id === queueSelectedId) || queueList[0] || null;
  useEffect(() => {
    if (qSel && qSel.record_id !== queueSelectedId) setQueueSelectedId(qSel.record_id);
  }, [qSel && qSel.record_id]); // eslint-disable-line react-hooks/exhaustive-deps

  const draftOf = id => drafts[id] || {
    cls: null,
    comment: ""
  };
  const setDraft = (id, patch) => setDrafts(prev => ({
    ...prev,
    [id]: {
      ...draftOf(id),
      ...patch
    }
  }));
  const commitPending = () => {
    const pending = pendingRef.current;
    if (!pending) return;
    clearTimeout(pending.timer);
    pendingRef.current = null;
    pending.commit();
  };
  // Закрытие вкладки не теряет решение: отправляем его немедленно.
  useEffect(() => {
    const flush = () => {
      const pending = pendingRef.current;
      if (!pending) return;
      clearTimeout(pending.timer);
      pendingRef.current = null;
      try {
        navigator.sendBeacon(`${API}/records/verdict`, new Blob([JSON.stringify({
          record_ids: pending.ids,
          classification: pending.cls,
          comment: pending.comment,
          source: "queue"
        })], {
          type: "application/json"
        }));
      } catch {/* браузер без sendBeacon — решение останется в очереди */}
    };
    window.addEventListener("pagehide", flush);
    return () => {
      window.removeEventListener("pagehide", flush);
      flush();
    };
  }, []);

  // Копии записи, которые ещё ждут решения в очереди.
  const queueCopies = rec => (rec.copy_ids || []).filter(id => queueRecords.some(q => q.record_id === id) && !hiddenQueueIds.has(id));
  const saveDecision = () => {
    const rec = qSel;
    if (!rec || !canMarkVerdict) return;
    const d = draftOf(rec.record_id);
    const comment = (d.comment || "").trim();
    if (!d.cls || !comment) {
      if (commentRef.current) commentRef.current.focus();
      return;
    }
    commitPending();
    // Точные копии в очереди решаются вместе с записью, если эксперт не снял галочку.
    const copies = d.applyCopies === false ? [] : queueCopies(rec);
    const payload = {
      id: rec.record_id,
      ids: [rec.record_id, ...copies],
      cls: d.cls,
      comment
    };
    const i = queueList.findIndex(r => r.record_id === rec.record_id);
    const rest = queueList.filter(r => !payload.ids.includes(r.record_id));
    const next = rest.find((r, k) => queueList.indexOf(r) > i) || rest[rest.length - 1] || null;
    setHiddenQueueIds(prev => new Set([...prev, ...payload.ids]));
    setQueueSelectedId(next ? next.record_id : null);
    setDrafts(prev => {
      const copy = {
        ...prev
      };
      delete copy[payload.id];
      return copy;
    });
    const restore = () => setHiddenQueueIds(prev => {
      const copy = new Set(prev);
      payload.ids.forEach(id => copy.delete(id));
      return copy;
    });
    const commit = async () => {
      const ok = await markVerdict(payload.ids, payload.cls, payload.comment, {
        quiet: true,
        source: "queue"
      });
      if (ok) {
        setReviewedCount(n => n + 1);
        loadQueue();
        loadSummary();
        // Решение из очереди меняет выборку «Базы» — перечитываем её с первой страницы.
        if (page === 0) loadRecords();else setPage(0);
      } else restore();
    };
    const timer = setTimeout(() => {
      if (pendingRef.current && pendingRef.current.id === payload.id) {
        pendingRef.current = null;
        commit();
      }
    }, 10000);
    pendingRef.current = {
      ...payload,
      timer,
      commit
    };
    const many = payload.ids.length > 1 ? ` для ${fmtInt(payload.ids.length)} ${lpPlural(payload.ids.length, "записи", "записей", "записей")}` : "";
    showToast(`Решение «${KIND_LABELS[payload.cls][0]}»${many} запишется через 10 секунд`, "info", {
      ttl: 10000,
      undo: () => {
        if (!pendingRef.current || pendingRef.current.id !== payload.id) return;
        clearTimeout(pendingRef.current.timer);
        pendingRef.current = null;
        restore();
        setQueueSelectedId(payload.id);
        setDrafts(prev => ({
          ...prev,
          [payload.id]: {
            cls: payload.cls,
            comment: payload.comment,
            applyCopies: payload.ids.length > 1 || undefined
          }
        }));
      }
    });
  };
  const moveQueue = delta => {
    if (!queueList.length || !qSel) return;
    const i = queueList.findIndex(r => r.record_id === qSel.record_id);
    const next = queueList[Math.max(0, Math.min(queueList.length - 1, i + delta))];
    if (next) {
      setQueueSelectedId(next.record_id);
      const node = document.getElementById(`lp-qi-${next.record_id}`);
      if (node) node.scrollIntoView({
        block: "nearest"
      });
    }
  };

  // ── Доступ: отзыв роли с отменой вместо окна подтверждения ────────────────
  const openAccess = () => {
    setAccessOpen(true);
    loadAdmin();
  };
  const revokeLater = username => {
    setHiddenExperts(prev => new Set(prev).add(username));
    const timer = setTimeout(() => {
      delete revokeTimersRef.current[username];
      revokeRole(username).finally(() => setHiddenExperts(prev => {
        const copy = new Set(prev);
        copy.delete(username);
        return copy;
      }));
    }, 10000);
    revokeTimersRef.current[username] = timer;
    showToast(`Роль эксперта ЦК КС отзывается у ${username}`, "info", {
      ttl: 10000,
      undo: () => {
        clearTimeout(revokeTimersRef.current[username]);
        delete revokeTimersRef.current[username];
        setHiddenExperts(prev => {
          const copy = new Set(prev);
          copy.delete(username);
          return copy;
        });
      }
    });
  };
  useFocusLayer(accessOpen, accessSheetRef, () => setAccessOpen(false));

  // ── Клавиши: J/K — по списку, / — поиск, 1/2/3 и ⌘Enter — решение ────────
  useEffect(() => {
    const onKey = e => {
      if (e.key === "Escape") {
        if (pop) {
          setPop(null);
          return;
        }
        if (view === "catalog" && readerOpen && window.innerWidth < 900) {
          setReaderOpen(false);
          return;
        }
        if (view === "queue" && qOpen && window.innerWidth < 900) {
          setQOpen(false);
          return;
        }
      }
      if (_lpLayerStack.length) return;
      const typing = e.target && e.target.matches && e.target.matches("input, textarea, select");
      if (view === "queue" && (e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        saveDecision();
        return;
      }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      const key = e.key.toLowerCase();
      if (view === "catalog") {
        if (key === "j" || key === "о") {
          e.preventDefault();
          moveRecord(1);
        } else if (key === "k" || key === "л") {
          e.preventDefault();
          moveRecord(-1);
        } else if (e.key === "/") {
          e.preventDefault();
          if (searchRef.current) searchRef.current.focus();
        }
      }
      if (view === "queue") {
        if (key === "j" || key === "о") {
          e.preventDefault();
          moveQueue(1);
        } else if (key === "k" || key === "л") {
          e.preventDefault();
          moveQueue(-1);
        } else if (["1", "2", "3"].includes(e.key) && qSel && canMarkVerdict) {
          setDraft(qSel.record_id, {
            cls: ["vulnerability", "fraud_scheme", "not_confirmed"][Number(e.key) - 1]
          });
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  // Всплывающие панели закрываются кликом мимо и прокруткой.
  useEffect(() => {
    if (!pop) return undefined;
    const onDown = e => {
      if (!e.target.closest(".lp-pop, [data-pop-anchor]")) setPop(null);
    };
    const onScroll = e => {
      if (!(e.target.closest && e.target.closest(".lp-pop"))) setPop(null);
    };
    document.addEventListener("mousedown", onDown);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [pop]);

  // ── Авторизация: fail-closed поверхности без защищённых данных ────────────
  if (authz === null) {
    return /*#__PURE__*/React.createElement("div", {
      className: "lp-state"
    }, /*#__PURE__*/React.createElement("p", {
      className: "lp-state-x"
    }, "\u041F\u0440\u043E\u0432\u0435\u0440\u044F\u0435\u043C \u0434\u043E\u0441\u0442\u0443\u043F\u2026"));
  }
  if (authz === false) {
    return /*#__PURE__*/React.createElement("div", {
      className: "lp-state"
    }, /*#__PURE__*/React.createElement("div", {
      className: "lp-state-ic"
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "shield",
      size: 20
    })), /*#__PURE__*/React.createElement("h1", {
      className: "lp-state-t"
    }, "\u041D\u0435\u0442 \u0434\u043E\u0441\u0442\u0443\u043F\u0430 \u043A \u043C\u043E\u0434\u0443\u043B\u044E \xAB\u0423\u044F\u0437\u0432\u0438\u043C\u043E\u0441\u0442\u0438\xBB"), /*#__PURE__*/React.createElement("p", {
      className: "lp-state-x"
    }, "\u0423\u0447\u0451\u0442\u043D\u0430\u044F \u0437\u0430\u043F\u0438\u0441\u044C \u043D\u0435 \u0430\u0432\u0442\u043E\u0440\u0438\u0437\u043E\u0432\u0430\u043D\u0430. \u041E\u0431\u0440\u0430\u0442\u0438\u0442\u0435\u0441\u044C \u043A \u0430\u0434\u043C\u0438\u043D\u0438\u0441\u0442\u0440\u0430\u0442\u043E\u0440\u0443 \u043C\u043E\u0434\u0443\u043B\u044F."));
  }
  if (authz === "error") {
    return /*#__PURE__*/React.createElement("div", {
      className: "lp-state",
      role: "alert"
    }, /*#__PURE__*/React.createElement("div", {
      className: "lp-state-ic lp-state-ic-err"
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "alert",
      size: 20
    })), /*#__PURE__*/React.createElement("h1", {
      className: "lp-state-t"
    }, "\u0421\u0435\u0440\u0432\u0438\u0441 \u043D\u0435\u0434\u043E\u0441\u0442\u0443\u043F\u0435\u043D"), /*#__PURE__*/React.createElement("p", {
      className: "lp-state-x"
    }, "\u041D\u0435 \u0443\u0434\u0430\u043B\u043E\u0441\u044C \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044C \u0440\u0430\u0431\u043E\u0447\u0438\u0435 \u043A\u043E\u043D\u0442\u0435\u043A\u0441\u0442\u044B. \u041F\u0440\u043E\u0432\u0435\u0440\u044C\u0442\u0435 \u0441\u043E\u0435\u0434\u0438\u043D\u0435\u043D\u0438\u0435 \u0438 \u043F\u043E\u0432\u0442\u043E\u0440\u0438\u0442\u0435."), /*#__PURE__*/React.createElement("div", {
      className: "lp-state-a"
    }, /*#__PURE__*/React.createElement("button", {
      className: "lp-btn lp-btn-primary",
      onClick: () => {
        setAuthz(null);
        setContextsRetry(n => n + 1);
      }
    }, "\u041F\u043E\u0432\u0442\u043E\u0440\u0438\u0442\u044C")));
  }

  // ── Части страницы ──────────────────────────────────────────────────────────
  const TAB_LABELS = {
    catalog: "База",
    ai_research: "Исследовать",
    queue: "Очередь",
    sources: "Добавить источник"
  };
  const tabContexts = authz.contexts.filter(c => c.id !== "admin");
  const awaitingTotal = totals ? totals.awaiting : null;
  const pageHead = /*#__PURE__*/React.createElement("header", {
    className: "lp-ph"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-ph-main"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-eyebrow"
  }, "\u0410\u043D\u0430\u043B\u0438\u0437 \xB7 \u0441\u0445\u0435\u043C\u044B \u0438 \u043B\u0430\u0437\u0435\u0439\u043A\u0438"), /*#__PURE__*/React.createElement("h1", {
    className: "lp-ph-t"
  }, "\u0423\u044F\u0437\u0432\u0438\u043C\u043E\u0441\u0442\u0438"), /*#__PURE__*/React.createElement("p", {
    className: "lp-ph-meta"
  }, "\u041B\u0430\u0437\u0435\u0439\u043A\u0438 \u0438 \u043C\u043E\u0448\u0435\u043D\u043D\u0438\u0447\u0435\u0441\u043A\u0438\u0435 \u0441\u0445\u0435\u043C\u044B \u0432 \u043F\u0440\u043E\u0434\u0443\u043A\u0442\u0430\u0445 \u0431\u0430\u043D\u043A\u043E\u0432. \u0417\u0430\u043F\u0438\u0441\u0438 \u0441\u043E\u0431\u0438\u0440\u0430\u044E\u0442\u0441\u044F \u0438\u0437 \u043E\u0431\u0441\u0443\u0436\u0434\u0435\u043D\u0438\u0439 \u043D\u0430 \u0444\u043E\u0440\u0443\u043C\u0430\u0445 \u0438 \u0432 \u0441\u043E\u0446\u0441\u0435\u0442\u044F\u0445, \u043D\u043E\u0432\u043E\u0441\u0442\u0435\u0439 \u0438 \u0441\u0430\u0439\u0442\u043E\u0432 \u0431\u0430\u043D\u043A\u043E\u0432; \u043C\u043E\u0434\u0435\u043B\u044C \u043E\u0442\u043C\u0435\u0447\u0430\u0435\u0442 \u0432\u043E\u0437\u043C\u043E\u0436\u043D\u044B\u0435 \u043D\u0430\u0445\u043E\u0434\u043A\u0438, \u043E\u043A\u043E\u043D\u0447\u0430\u0442\u0435\u043B\u044C\u043D\u044B\u0439 \u0432\u0435\u0440\u0434\u0438\u043A\u0442 \u0432\u044B\u043D\u043E\u0441\u0438\u0442 \u044D\u043A\u0441\u043F\u0435\u0440\u0442 \u0426\u041A \u041A\u0421.")), canAdmin && /*#__PURE__*/React.createElement("div", {
    className: "lp-ph-act"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn",
    onClick: openAccess
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "users"
  }), "\u0414\u043E\u0441\u0442\u0443\u043F")));
  const tabsBar = /*#__PURE__*/React.createElement("nav", {
    className: "lp-tabs"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-tabs-l",
    role: "tablist",
    "aria-label": "\u0420\u0430\u0437\u0434\u0435\u043B\u044B \u0432\u043A\u043B\u0430\u0434\u043A\u0438",
    "aria-orientation": "horizontal"
  }, tabContexts.map(c => {
    const active = c.id === view;
    const count = c.id === "catalog" && totals ? totals.total : c.id === "queue" && awaitingTotal ? awaitingTotal : null;
    return /*#__PURE__*/React.createElement("button", {
      key: c.id,
      type: "button",
      role: "tab",
      id: `lp-tab-${c.id}`,
      "aria-selected": active,
      "aria-controls": `lp-panel-${c.id}`,
      tabIndex: active ? 0 : -1,
      "data-context-id": c.id,
      ref: c.id === "sources" ? sourcesTabRef : c.id === "ai_research" ? researchTabRef : null,
      className: "lp-tab" + (active ? " lp-tab-on" : ""),
      onKeyDown: onContextTabKeyDown,
      onClick: () => {
        setPop(null);
        openContext(c.id);
      }
    }, TAB_LABELS[c.id] || c.title, count != null && /*#__PURE__*/React.createElement("span", {
      className: "lp-tab-n" + (c.id === "queue" ? " lp-tab-hot" : "")
    }, fmtInt(count)));
  })), /*#__PURE__*/React.createElement("div", {
    className: "lp-tabs-r"
  }, view === "catalog" && /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn-text",
    "data-pop-anchor": "method",
    "aria-label": "\u041A\u0430\u043A \u0447\u0438\u0442\u0430\u0442\u044C \u0432\u0435\u0440\u0434\u0438\u043A\u0442",
    onClick: e => {
      const rect = e.currentTarget.getBoundingClientRect();
      setPop(p => p && p.kind === "method" ? null : {
        kind: "method",
        rect
      });
    }
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "info",
    size: 14
  }), /*#__PURE__*/React.createElement("span", {
    className: "lp-wide"
  }, "\u041A\u0430\u043A \u0447\u0438\u0442\u0430\u0442\u044C \u0432\u0435\u0440\u0434\u0438\u043A\u0442")), view === "queue" && reviewedCount > 0 && /*#__PURE__*/React.createElement("span", {
    className: "lp-tnum"
  }, "\u0420\u0430\u0437\u043E\u0431\u0440\u0430\u043D\u043E: ", reviewedCount), view === "ai_research" && /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-btn-sm lp-rs-hb",
    onClick: () => setHistOpen(o => !o)
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "hist",
    size: 14
  }), "\u0418\u0441\u0442\u043E\u0440\u0438\u044F")));
  const tabPlaceholders = authz.contexts.filter(c => c.id !== view && c.id !== "admin").map(c => /*#__PURE__*/React.createElement("section", {
    key: `lp-panel-placeholder-${c.id}`,
    id: `lp-panel-${c.id}`,
    role: "tabpanel",
    "aria-labelledby": `lp-tab-${c.id}`,
    hidden: true
  }));

  // ── База: сводка ───────────────────────────────────────────────────────────
  const kpi = ({
    on,
    onClick,
    label,
    value,
    sub,
    tone
  }) => /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-kpi" + (on ? " lp-kpi-on" : ""),
    "aria-pressed": on,
    onClick: onClick
  }, /*#__PURE__*/React.createElement("span", {
    className: "lp-kl"
  }, tone && /*#__PURE__*/React.createElement("span", {
    className: "lp-sw lp-sw-" + tone
  }), label), /*#__PURE__*/React.createElement("span", {
    className: "lp-kv"
  }, value == null ? "—" : fmtInt(value)), /*#__PURE__*/React.createElement("span", {
    className: "lp-ks"
  }, sub));
  const reviewedLine = (all, awaitingN) => !all ? "пока нет" : `проверено экспертом: ${fmtInt(all - awaitingN)} из ${fmtInt(all)}`;
  const kpis = /*#__PURE__*/React.createElement("div", {
    className: "lp-kpis"
  }, kpi({
    on: fVerification === "awaiting",
    label: "Ждут проверки",
    value: totals && totals.awaiting,
    sub: totals ? `из ${fmtInt(totals.total)} ${lpPlural(totals.total, "записи", "записей", "записей")} в базе` : "…",
    onClick: () => setFVerification(v => v === "awaiting" ? "all" : "awaiting")
  }), kpi({
    on: fPeriod === "7",
    label: "Новые находки за 7 дней",
    value: totals && totals.new_7d,
    sub: totals ? `на прошлой неделе: ${fmtInt(totals.new_prev_7d)}` : "…",
    onClick: () => applyPeriod(fPeriod === "7" ? "all" : "7")
  }), kpi({
    on: fClassification === "vulnerability",
    label: "Уязвимости",
    tone: "neg",
    value: totals && totals.vulnerability,
    sub: totals ? reviewedLine(totals.vulnerability, totals.awaiting_vulnerability) : "…",
    onClick: () => setFClassification(c => c === "vulnerability" ? "confirmed" : "vulnerability")
  }), kpi({
    on: fClassification === "fraud_scheme",
    label: "Мошеннические схемы",
    tone: "legal",
    value: totals && totals.fraud_scheme,
    sub: totals ? reviewedLine(totals.fraud_scheme, totals.awaiting_fraud_scheme) : "…",
    onClick: () => setFClassification(c => c === "fraud_scheme" ? "confirmed" : "fraud_scheme")
  }));

  // ── База: фильтры ──────────────────────────────────────────────────────────
  const seg = (label, value, options, onPick) => /*#__PURE__*/React.createElement("div", {
    className: "lp-seg",
    role: "group",
    "aria-label": label
  }, options.map(([v, text, n]) => /*#__PURE__*/React.createElement("button", {
    key: v,
    type: "button",
    className: "lp-seg-btn" + (value === v ? " lp-seg-on" : ""),
    "aria-pressed": value === v,
    onClick: () => onPick(v)
  }, text, n != null && /*#__PURE__*/React.createElement("span", {
    className: "lp-seg-n"
  }, fmtInt(n)))));
  const types = facets ? facets.types : null;
  const bankFacet = facets ? facets.banks : [];
  const bankCount = slug => {
    const hit = bankFacet.find(b => b.slug === slug);
    return hit ? hit.count : 0;
  };
  const bankLabel = fBanks.length === 0 ? "Все банки" : fBanks.length === 1 ? bankName(fBanks[0]) : `Банки · ${fBanks.length}`;
  const chips = [];
  if (fClassification !== "confirmed") chips.push(["type", fClassification === "all" ? "Все записи" : (KIND_LABELS[fClassification] || [fClassification])[0]]);
  fBanks.forEach(b => chips.push(["bank:" + b, bankName(b)]));
  if (fFrom || fTo) chips.push(["period", fPeriod !== "custom" && fPeriod !== "all" ? `за ${fPeriod} ${lpPlural(Number(fPeriod), "день", "дня", "дней")}` : `${fFrom ? fmtDay(fFrom) : "…"} — ${fTo ? fmtDay(fTo) : "…"}`]);
  if (fVerification !== "all") chips.push(["check", fVerification === "awaiting" ? "Ждут проверки" : fVerification === "reviewed" ? "Проверено" : fVerification]);
  if (fText.trim()) chips.push(["q", `«${fText.trim()}»`]);
  const dropChip = key => {
    if (key === "type") setFClassification("confirmed");else if (key.startsWith("bank:")) setFBanks(prev => prev.filter(b => b !== key.slice(5)));else if (key === "period") applyPeriod("all");else if (key === "check") setFVerification("all");else if (key === "q") setFText("");
  };
  const filters = /*#__PURE__*/React.createElement("div", {
    className: "lp-fhead"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-srow"
  }, /*#__PURE__*/React.createElement("label", {
    className: "lp-search"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "search"
  }), /*#__PURE__*/React.createElement("span", {
    className: "lp-sr-only"
  }, "\u041F\u043E\u0438\u0441\u043A \u043F\u043E \u0442\u0435\u043A\u0441\u0442\u0443"), /*#__PURE__*/React.createElement("input", {
    id: "lp-filter-text",
    ref: searchRef,
    type: "search",
    value: fText,
    onChange: e => setFText(e.target.value),
    autoComplete: "off",
    placeholder: "\u041F\u043E\u0438\u0441\u043A: \xAB\u043A\u044D\u0448\u0431\u044D\u043A \u0421\u0411\u041F\xBB, \xAB\u043E\u0431\u043D\u0430\u043B\u0438\u0447\u0438\u0432\u0430\u043D\u0438\u0435\xBB, \xAB\u0441\u0430\u043C\u043E\u0437\u0430\u043F\u0440\u0435\u0442\xBB"
  }), /*#__PURE__*/React.createElement("span", {
    className: "lp-kbd",
    "aria-hidden": "true"
  }, "/")), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-fbtn",
    "aria-expanded": showFilters,
    onClick: () => setShowFilters(s => !s)
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "filter"
  }), "\u0424\u0438\u043B\u044C\u0442\u0440\u044B", activeFilterCount ? ` · ${activeFilterCount}` : "")), /*#__PURE__*/React.createElement("div", {
    className: "lp-frow" + (showFilters ? " lp-frow-show" : "")
  }, seg("Тип записи", fClassification, [["confirmed", "Уязвимости и схемы", types && types.confirmed], ["vulnerability", "Уязвимости", types && types.vulnerability], ["fraud_scheme", "Схемы", types && types.fraud_scheme], ["not_confirmed", "Не подтверждено", types && types.not_confirmed], ["all", "Все", types && types.all]], setFClassification), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-dd" + (fBanks.length ? " lp-dd-on" : ""),
    "data-pop-anchor": "banks",
    "aria-haspopup": "true",
    "aria-expanded": !!(pop && pop.kind === "banks"),
    onClick: e => {
      const rect = e.currentTarget.getBoundingClientRect();
      setPop(p => p && p.kind === "banks" ? null : {
        kind: "banks",
        rect
      });
    }
  }, bankLabel, /*#__PURE__*/React.createElement(Icon, {
    name: "down",
    size: 14
  })), seg("Период публикации", fPeriod, [["7", "7 дней"], ["30", "30 дней"], ["90", "90 дней"], ["all", "Всё время"], ["custom", "Свой период"]], applyPeriod), seg("Проверка ЦК КС", fVerification === "verified" || fVerification === "pending" ? "all" : fVerification, [["all", "Все"], ["awaiting", "Ждут проверки", facets && facets.awaiting], ["reviewed", "Проверено"]], setFVerification)), fPeriod === "custom" && /*#__PURE__*/React.createElement("div", {
    className: "lp-period"
  }, /*#__PURE__*/React.createElement("label", {
    htmlFor: "lp-filter-from"
  }, "\u0414\u0430\u0442\u0430 \u043F\u0443\u0431\u043B\u0438\u043A\u0430\u0446\u0438\u0438 \u2014 \u0441"), /*#__PURE__*/React.createElement("input", {
    id: "lp-filter-from",
    type: "date",
    value: fFrom,
    onChange: e => setFFrom(e.target.value)
  }), /*#__PURE__*/React.createElement("label", {
    htmlFor: "lp-filter-to"
  }, "\u0414\u0430\u0442\u0430 \u043F\u0443\u0431\u043B\u0438\u043A\u0430\u0446\u0438\u0438 \u2014 \u043F\u043E"), /*#__PURE__*/React.createElement("input", {
    id: "lp-filter-to",
    type: "date",
    value: fTo,
    onChange: e => setFTo(e.target.value)
  })), chips.length > 0 && /*#__PURE__*/React.createElement("div", {
    className: "lp-fchips"
  }, chips.map(([key, text]) => /*#__PURE__*/React.createElement("span", {
    key: key,
    className: "lp-fchip"
  }, text, /*#__PURE__*/React.createElement("button", {
    type: "button",
    "aria-label": `Убрать фильтр ${text}`,
    onClick: () => dropChip(key)
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "x",
    size: 12
  })))), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn-text",
    onClick: resetAll
  }, "\u0421\u0431\u0440\u043E\u0441\u0438\u0442\u044C \u0432\u0441\u0435")));

  // ── Карточка записи (база и очередь) ───────────────────────────────────────
  const confBar = (value, large) => {
    const p = pctOf(value);
    return /*#__PURE__*/React.createElement("span", {
      className: "lp-cbar" + (large ? " lp-cbar-lg" : ""),
      "aria-hidden": "true"
    }, /*#__PURE__*/React.createElement("i", {
      style: {
        width: `${p || 0}%`
      }
    }));
  };
  const kindWord = kind => (KIND_LABELS[kind] || KIND_LABELS.none)[0].toLowerCase();
  const historyEvents = r => {
    const events = [];
    const expert = r.expert_decisions || [];
    const domain = r.domain || hostOf(r.url);
    if (r.published_at) events.push({
      at: r.published_at,
      t: `Опубликовано${domain ? ` на ${domain}` : ""}`
    });
    events.push({
      at: r.collected_at,
      t: r.provenance ? "Найдено AI-исследованием и добавлено в базу" : "Собрано в общую базу"
    });
    const classifierText = r.classifier_verdict_reason || (r.verdict_model !== "manual" ? r.verdict_reason : null);
    if (r.verdict_model && r.verdict_model !== "manual") {
      events.push({
        at: r.classified_at || r.collected_at,
        t: `Модель: ${KIND_LABELS[recordKind(r)][0].toLowerCase()}` + (pctOf(r.verdict_confidence) != null ? `, вероятность ${pctOf(r.verdict_confidence)}%` : ""),
        c: classifierText && !/^Предварительн/.test(classifierText) ? classifierText : null
      });
    } else if (r.verdict_model === "manual") {
      if (classifierText) events.push({
        at: r.collected_at,
        t: "Модель отметила запись",
        c: classifierText
      });
      if (!expert.length) {
        const manual = r.verdict_reason && !/^manual:/.test(r.verdict_reason) ? r.verdict_reason : null;
        events.push({
          at: r.classified_at,
          t: `Эксперт ЦК КС: ${kindWord(recordKind(r))}`,
          c: manual,
          key: true
        });
      }
    }
    // Журнал решений: кто решил, что было и что стало.
    expert.forEach(d => events.push({
      at: d.decided_at,
      t: `Решение ЦК КС (${d.decided_by === "anonymous" ? "без авторизации" : d.decided_by}): ` + (d.previous && d.previous !== d.decision ? `${kindWord(d.previous)} → ` : "") + kindWord(d.decision),
      c: d.comment,
      key: true
    }));
    (r.decisions || []).forEach(d => events.push({
      at: d.decided_at,
      t: `Решение ЦК КС (${d.decided_by}): ${decisionLabel(d.decision).toLowerCase()}`,
      c: d.comment,
      key: true
    }));
    return events.filter(e => e.at || e.key);
  };
  const recordBody = (base, ctx) => {
    if (!base) return null;
    const entry = contentCache[base.record_id];
    const detail = entry && entry.data ? entry.data : null;
    const r = {
      ...base,
      ...(detail || {})
    };
    const kind = recordKind(r);
    const positive = POSITIVE_KINDS.has(kind);
    const manual = r.verdict_model === "manual";
    const reviewed = r.reviewed === true || manual;
    const awaiting = !reviewed && (r.awaiting === true || r.is_loophole === true && !manual);
    const sum = summaries[r.record_id];
    const summaryText = r.summary || sum && sum.text;
    const headline = r.headline || sum && sum.headline;
    const doubt = String(r.summary_doubt || sum && sum.doubt || "").replace(/\.$/, "");
    const bank = knownBank(r.bank_slug);
    // Заголовок находки от модели; название ветки форума — строкой ниже.
    const topic = headline && r.title && r.title.trim() !== headline ? r.title.trim() : null;
    const copyIds = r.copy_ids || [];
    const classifierText = r.classifier_verdict_reason || r.verdict_reason;
    const domain = r.domain || hostOf(r.url);
    const fragment = (r.snippet || "").trim();
    const fullText = detail && detail.raw_text;
    const lenKb = detail && detail.raw_text_len ? Math.max(1, Math.round(detail.raw_text_len / 1000)) : null;
    const failed = detail && (detail.content_status === "fetch_failed" || detail.content_status === "empty");
    const truncated = detail && detail.content_status === "truncated";
    const isFull = fullOpen.has(r.record_id);
    const conf = manual ? null : pctOf(r.verdict_confidence);
    return /*#__PURE__*/React.createElement("div", {
      className: "lp-rd-body"
    }, /*#__PURE__*/React.createElement("div", {
      className: "lp-rd-meta"
    }, [bank && /*#__PURE__*/React.createElement("span", {
      key: "b",
      className: bankClass(r.bank_slug),
      title: r.bank_inferred ? "Банк определён моделью по тексту записи" : undefined
    }, bank, r.bank_inferred && /*#__PURE__*/React.createElement("span", {
      className: "lp-sr-only"
    }, " (\u043E\u043F\u0440\u0435\u0434\u0435\u043B\u0451\u043D \u043F\u043E \u0442\u0435\u043A\u0441\u0442\u0443)")), domain && /*#__PURE__*/React.createElement("span", {
      key: "d"
    }, domain), r.url && /*#__PURE__*/React.createElement("a", {
      key: "u",
      className: "lp-link",
      href: r.url,
      target: "_blank",
      rel: "noopener noreferrer"
    }, "\u043E\u0442\u043A\u0440\u044B\u0442\u044C \u0438\u0441\u0442\u043E\u0447\u043D\u0438\u043A", /*#__PURE__*/React.createElement(Icon, {
      name: "ext",
      size: 12
    }))].filter(Boolean).flatMap((node, i) => i ? [/*#__PURE__*/React.createElement("span", {
      key: "s" + i,
      "aria-hidden": "true"
    }, "\xB7"), node] : [node])), /*#__PURE__*/React.createElement("h2", {
      className: "lp-rd-title",
      tabIndex: -1
    }, headline || r.title || r.snippet || "Без заголовка"), topic && /*#__PURE__*/React.createElement("p", {
      className: "lp-rd-topic"
    }, "\u0422\u0435\u043C\u0430: ", topic), /*#__PURE__*/React.createElement("div", {
      className: "lp-rd-badges"
    }, /*#__PURE__*/React.createElement(KindBadge, {
      kind: kind
    }), reviewed ? /*#__PURE__*/React.createElement("span", {
      className: "lp-st lp-st-done"
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "check",
      size: 14
    }), "\u041F\u0440\u043E\u0432\u0435\u0440\u0435\u043D\u043E \u044D\u043A\u0441\u043F\u0435\u0440\u0442\u043E\u043C", r.classified_at && manual ? ` · ${fmtDay(r.classified_at)}` : "") : awaiting ? /*#__PURE__*/React.createElement("span", {
      className: "lp-st lp-st-pend"
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "clock",
      size: 14
    }), "\u0416\u0434\u0451\u0442 \u043F\u0440\u043E\u0432\u0435\u0440\u043A\u0438 \u044D\u043A\u0441\u043F\u0435\u0440\u0442\u043E\u043C \u0426\u041A \u041A\u0421") : null), positive && doubt && /*#__PURE__*/React.createElement("div", {
      className: "lp-callout lp-callout-doubt"
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "info"
    }), /*#__PURE__*/React.createElement("span", null, /*#__PURE__*/React.createElement("b", null, "\u041C\u043E\u0434\u0435\u043B\u044C \u0441\u043E\u043C\u043D\u0435\u0432\u0430\u0435\u0442\u0441\u044F:"), " ", doubt, ". \u042D\u0442\u043E \u043F\u043E\u0434\u0441\u043A\u0430\u0437\u043A\u0430 \u044D\u043A\u0441\u043F\u0435\u0440\u0442\u0443, \u0432\u0435\u0440\u0434\u0438\u043A\u0442 \u043D\u0435 \u043C\u0435\u043D\u044F\u0435\u0442\u0441\u044F.")), ctx === "base" && awaiting && /*#__PURE__*/React.createElement("div", {
      className: "lp-callout"
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "clock"
    }), /*#__PURE__*/React.createElement("span", null, canQueue ? "Запись ждёт решения эксперта. Вердикт модели пока предварительный." : "Запись ещё не проверил эксперт ЦК КС. Вердикт модели предварительный."), canQueue && /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-btn lp-btn-primary lp-btn-sm",
      onClick: () => {
        setView("queue");
        setQueueSelectedId(r.record_id);
        setQOpen(true);
        loadQueue();
      }
    }, "\u0420\u0435\u0448\u0438\u0442\u044C \u0432 \u043E\u0447\u0435\u0440\u0435\u0434\u0438", /*#__PURE__*/React.createElement(Icon, {
      name: "right",
      size: 14
    }))), /*#__PURE__*/React.createElement("dl", {
      className: "lp-facts"
    }, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("dt", null, "\u041E\u043F\u0443\u0431\u043B\u0438\u043A\u043E\u0432\u0430\u043D\u043E"), /*#__PURE__*/React.createElement("dd", null, r.published_at ? fmtDate(r.published_at) : "дата не найдена")), /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("dt", null, "\u0421\u043E\u0431\u0440\u0430\u043D\u043E"), /*#__PURE__*/React.createElement("dd", null, fmtDate(r.collected_at))), /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("dt", null, manual ? "Вердикт" : positive ? "Вероятность" : "Вердикт модели"), /*#__PURE__*/React.createElement("dd", null, manual ? "решение эксперта" : !positive ? kind === "none" ? "без вердикта" : "находки нет" : conf != null ? `${conf}% · ${confWord(r.verdict_confidence)}` : "нет оценки"))), positive ? /*#__PURE__*/React.createElement("section", {
      className: "lp-rsec"
    }, /*#__PURE__*/React.createElement("h3", null, "\u0421\u0443\u0442\u044C"), summaryText ? /*#__PURE__*/React.createElement("p", null, summaryText) : sum && sum.loading ? /*#__PURE__*/React.createElement("div", {
      className: "lp-sk-lines",
      "aria-label": "\u0421\u043E\u0441\u0442\u0430\u0432\u043B\u044F\u0435\u043C \u0441\u0443\u0442\u044C"
    }, /*#__PURE__*/React.createElement("span", {
      className: "lp-sk"
    }), /*#__PURE__*/React.createElement("span", {
      className: "lp-sk"
    }), /*#__PURE__*/React.createElement("span", {
      className: "lp-sk lp-sk-short"
    })) : /*#__PURE__*/React.createElement("p", {
      className: "lp-muted-p"
    }, classifierText || "Суть пока не составлена.")) : classifierText && !/^manual:/.test(classifierText) ? /*#__PURE__*/React.createElement("section", {
      className: "lp-rsec"
    }, /*#__PURE__*/React.createElement("h3", null, "\u041A\u043E\u043C\u043C\u0435\u043D\u0442\u0430\u0440\u0438\u0439 \u043A\u043B\u0430\u0441\u0441\u0438\u0444\u0438\u043A\u0430\u0442\u043E\u0440\u0430"), /*#__PURE__*/React.createElement("p", {
      className: "lp-cmt"
    }, classifierText)) : null, /*#__PURE__*/React.createElement("section", {
      className: "lp-rsec"
    }, /*#__PURE__*/React.createElement("h3", null, "\u0424\u0440\u0430\u0433\u043C\u0435\u043D\u0442 \u0438\u0441\u0442\u043E\u0447\u043D\u0438\u043A\u0430"), fragment ? /*#__PURE__*/React.createElement("blockquote", {
      className: "lp-quote"
    }, fragment) : /*#__PURE__*/React.createElement("p", {
      className: "lp-muted-p"
    }, "\u0424\u0440\u0430\u0433\u043C\u0435\u043D\u0442\u0430 \u043D\u0435\u0442."), /*#__PURE__*/React.createElement("div", {
      className: "lp-q-foot"
    }, !detail && entry && entry.loading && /*#__PURE__*/React.createElement("span", null, "\u0417\u0430\u0433\u0440\u0443\u0436\u0430\u0435\u043C \u0442\u0435\u043A\u0441\u0442\u2026"), entry && entry.error && /*#__PURE__*/React.createElement("span", {
      className: "lp-warn-t"
    }, "\u0422\u0435\u043A\u0441\u0442 \u043D\u0435 \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u043B\u0441\u044F: ", entry.error), failed && /*#__PURE__*/React.createElement("span", {
      className: "lp-warn-t"
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "alert",
      size: 13
    }), "\u041F\u043E\u043B\u043D\u044B\u0439 \u043A\u043E\u043D\u0442\u0435\u043D\u0442 \u043D\u0435 \u0443\u0434\u0430\u043B\u043E\u0441\u044C \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044C; \u043F\u043E\u043A\u0430\u0437\u0430\u043D \u0441\u043E\u0445\u0440\u0430\u043D\u0451\u043D\u043D\u044B\u0439 \u0444\u0440\u0430\u0433\u043C\u0435\u043D\u0442."), truncated && /*#__PURE__*/React.createElement("span", null, "\u0422\u0435\u043A\u0441\u0442 \u0441\u043E\u0445\u0440\u0430\u043D\u0451\u043D \u043D\u0435 \u043F\u043E\u043B\u043D\u043E\u0441\u0442\u044C\u044E", lenKb ? `: до ${lenKb} тыс. знаков` : "", "."), fullText && !failed && /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-btn-text",
      "aria-expanded": isFull,
      onClick: () => setFullOpen(prev => {
        const s = new Set(prev);
        if (s.has(r.record_id)) s.delete(r.record_id);else s.add(r.record_id);
        return s;
      })
    }, isFull ? "Свернуть полный текст" : `Полный текст${lenKb ? ` · ${lenKb} тыс. знаков` : ""}`)), isFull && fullText && /*#__PURE__*/React.createElement("div", {
      className: "lp-fulltext"
    }, fullText)), !manual && positive && conf != null && /*#__PURE__*/React.createElement("section", {
      className: "lp-rsec"
    }, /*#__PURE__*/React.createElement("h3", null, "\u0423\u0432\u0435\u0440\u0435\u043D\u043D\u043E\u0441\u0442\u044C \u043C\u043E\u0434\u0435\u043B\u0438"), /*#__PURE__*/React.createElement("div", {
      className: "lp-confbig"
    }, confBar(r.verdict_confidence, true), /*#__PURE__*/React.createElement("b", null, confWord(r.verdict_confidence)), /*#__PURE__*/React.createElement("span", null, conf, "%")), /*#__PURE__*/React.createElement("p", {
      className: "lp-note"
    }, positive ? `Насколько запись похожа на ${KIND_LABELS[kind][0].toLowerCase()} по оценке модели.` : "Оценка модели для этой записи.", " \u042D\u0442\u043E \u043D\u0435 \u0432\u0435\u0440\u043E\u044F\u0442\u043D\u043E\u0441\u0442\u044C \u0443\u0449\u0435\u0440\u0431\u0430 \u0438 \u043D\u0435 \u0440\u0435\u0448\u0435\u043D\u0438\u0435 \u044D\u043A\u0441\u043F\u0435\u0440\u0442\u0430.")), copyIds.length > 0 && /*#__PURE__*/React.createElement("section", {
      className: "lp-rsec"
    }, /*#__PURE__*/React.createElement("h3", null, "\u041A\u043E\u043F\u0438\u0438"), /*#__PURE__*/React.createElement("p", {
      className: "lp-muted-p"
    }, "\u0422\u043E\u0442 \u0436\u0435 \u0444\u0440\u0430\u0433\u043C\u0435\u043D\u0442 \u0435\u0441\u0442\u044C \u0435\u0449\u0451 \u0432 ", fmtInt(copyIds.length), " ", lpPlural(copyIds.length, "записи", "записях", "записях"), ":", " ", copyIds.map((id, i) => /*#__PURE__*/React.createElement(React.Fragment, {
      key: id
    }, i ? ", " : "", /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-btn-text lp-inline",
      onClick: () => openRecordInBase({
        record_id: id
      })
    }, "\u2116", id))))), /*#__PURE__*/React.createElement("section", {
      className: "lp-rsec"
    }, /*#__PURE__*/React.createElement("h3", null, "\u0418\u0441\u0442\u043E\u0440\u0438\u044F"), /*#__PURE__*/React.createElement("ol", {
      className: "lp-tl"
    }, historyEvents(r).map((e, i) => /*#__PURE__*/React.createElement("li", {
      key: i,
      className: e.key ? "lp-tl-key" : ""
    }, /*#__PURE__*/React.createElement("span", {
      className: "lp-tl-d"
    }, e.at ? fmtDate(e.at) : "—"), /*#__PURE__*/React.createElement("span", {
      className: "lp-tl-t"
    }, e.t), e.c && /*#__PURE__*/React.createElement("div", {
      className: "lp-tl-c"
    }, "\xAB", e.c, "\xBB"))))), ctx === "base" && canMarkVerdict && !awaiting && /*#__PURE__*/React.createElement("div", {
      className: "lp-rsec"
    }, /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-btn-text",
      onClick: () => {
        setMarkComment("");
        setVerdictModal({
          record: r
        });
      }
    }, "\u0418\u0437\u043C\u0435\u043D\u0438\u0442\u044C \u0432\u0435\u0440\u0434\u0438\u043A\u0442")));
  };
  const readerHead = (record, list, ctx) => {
    const i = list.findIndex(x => x.record_id === record.record_id);
    const total = ctx === "base" ? recordsTotal : queueCount;
    return /*#__PURE__*/React.createElement("div", {
      className: "lp-rd-head"
    }, /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-btn lp-btn-sm lp-rd-back",
      onClick: () => ctx === "base" ? setReaderOpen(false) : setQOpen(false)
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "left",
      size: 14
    }), ctx === "base" ? "База" : "Очередь"), /*#__PURE__*/React.createElement("div", {
      className: "lp-rd-nav"
    }, /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-icb",
      "aria-label": "\u041F\u0440\u0435\u0434\u044B\u0434\u0443\u0449\u0430\u044F \u0437\u0430\u043F\u0438\u0441\u044C",
      disabled: i <= 0,
      onClick: () => ctx === "base" ? moveRecord(-1) : moveQueue(-1)
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "up"
    })), /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-icb",
      "aria-label": "\u0421\u043B\u0435\u0434\u0443\u044E\u0449\u0430\u044F \u0437\u0430\u043F\u0438\u0441\u044C",
      disabled: i < 0 || i >= list.length - 1,
      onClick: () => ctx === "base" ? moveRecord(1) : moveQueue(1)
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "down"
    })), i >= 0 && /*#__PURE__*/React.createElement("span", {
      className: "lp-rd-pos"
    }, fmtInt(i + 1), " \u0438\u0437 ", fmtInt(total))), /*#__PURE__*/React.createElement("div", {
      className: "lp-rd-acts"
    }, /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-btn lp-btn-sm",
      "data-pop-anchor": "case",
      "aria-label": "\u0414\u043E\u0431\u0430\u0432\u0438\u0442\u044C \u0432 \u0430\u0443\u0434\u0438\u0442-\u0434\u0435\u043B\u043E",
      onClick: e => openCasePop(e, record)
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "case",
      size: 14
    }), /*#__PURE__*/React.createElement("span", {
      className: "lp-lbl"
    }, "\u0412 \u0434\u0435\u043B\u043E")), ctx === "base" && /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-btn lp-btn-sm",
      "aria-label": "\u0418\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u0442\u044C \u0433\u043B\u0443\u0431\u0436\u0435",
      onClick: () => deeperResearch(record)
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "spark",
      size: 14
    }), /*#__PURE__*/React.createElement("span", {
      className: "lp-lbl"
    }, "\u0418\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u0442\u044C \u0433\u043B\u0443\u0431\u0436\u0435"))));
  };

  // ── База: элемент списка ───────────────────────────────────────────────────
  const listItem = r => {
    const kind = recordKind(r);
    const positive = POSITIVE_KINDS.has(kind);
    const on = r.record_id === selRecordId;
    const manual = r.verdict_model === "manual";
    const awaiting = !manual && !r.reviewed && (r.awaiting === true || r.is_loophole === true);
    const line = positive ? r.summary || r.verdict_reason : null;
    // Вероятность — только у находок: у «не подтверждено» она читалась бы
    // как «вероятно уязвимость».
    const conf = manual || !positive ? null : pctOf(r.verdict_confidence);
    const bank = knownBank(r.bank_slug);
    return /*#__PURE__*/React.createElement("div", {
      key: r.record_id,
      id: `lp-item-${r.record_id}`,
      role: "listitem",
      className: "lp-c" + (on ? " lp-c-on" : "") + (selectMode ? " lp-c-chk" : ""),
      onClick: () => pickRecord(r.record_id)
    }, selectMode && /*#__PURE__*/React.createElement("label", {
      className: "lp-c-box",
      htmlFor: `lp-select-record-${r.record_id}`,
      onClick: e => e.stopPropagation()
    }, /*#__PURE__*/React.createElement("span", {
      className: "lp-sr-only"
    }, "\u0412\u044B\u0431\u0440\u0430\u0442\u044C \u0437\u0430\u043F\u0438\u0441\u044C"), /*#__PURE__*/React.createElement("input", {
      id: `lp-select-record-${r.record_id}`,
      type: "checkbox",
      checked: selected.has(r.record_id),
      onChange: () => toggleRow(r.record_id)
    })), /*#__PURE__*/React.createElement("div", {
      className: "lp-c-meta"
    }, bank && /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("span", {
      className: bankClass(r.bank_slug)
    }, bank), /*#__PURE__*/React.createElement("span", {
      "aria-hidden": "true"
    }, "\xB7")), /*#__PURE__*/React.createElement("span", {
      className: "lp-c-mt"
    }, [r.domain || hostOf(r.url), fmtDay(r.published_at || r.collected_at)].filter(Boolean).join(" · ")), /*#__PURE__*/React.createElement(KindBadge, {
      kind: kind
    })), /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-c-title",
      "aria-current": on ? "true" : undefined,
      onClick: e => {
        e.stopPropagation();
        pickRecord(r.record_id);
      }
    }, /*#__PURE__*/React.createElement(Hl, {
      text: r.headline || r.title || r.snippet || "Без заголовка",
      q: fText
    })), line ? /*#__PURE__*/React.createElement("div", {
      className: "lp-c-snip"
    }, /*#__PURE__*/React.createElement(Hl, {
      text: line,
      q: fText
    })) : r.snippet ? /*#__PURE__*/React.createElement("div", {
      className: "lp-c-quote"
    }, "\xAB", /*#__PURE__*/React.createElement(Hl, {
      text: r.snippet,
      q: fText
    }), "\xBB") : null, /*#__PURE__*/React.createElement("div", {
      className: "lp-c-sig"
    }, conf != null && /*#__PURE__*/React.createElement("span", {
      className: "lp-conf",
      title: `Предварительная вероятность ${conf}%`
    }, confBar(r.verdict_confidence), "\u0432\u0435\u0440\u043E\u044F\u0442\u043D\u043E\u0441\u0442\u044C ", confWord(r.verdict_confidence)), awaiting && /*#__PURE__*/React.createElement("span", {
      className: "lp-pend"
    }, "\u0436\u0434\u0451\u0442 \u043F\u0440\u043E\u0432\u0435\u0440\u043A\u0438"), (manual || r.reviewed) && /*#__PURE__*/React.createElement("span", {
      className: "lp-done"
    }, "\u043F\u0440\u043E\u0432\u0435\u0440\u0435\u043D\u043E"), r.provenance && /*#__PURE__*/React.createElement("span", null, "\u0438\u0437 \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u044F"), positive && r.summary_doubt && /*#__PURE__*/React.createElement("span", {
      className: "lp-doubt",
      title: r.summary_doubt
    }, "\u043C\u043E\u0434\u0435\u043B\u044C \u0441\u043E\u043C\u043D\u0435\u0432\u0430\u0435\u0442\u0441\u044F"), (r.copy_ids || []).length > 0 && /*#__PURE__*/React.createElement("span", null, "\u043A\u043E\u043F\u0438\u0439: ", fmtInt(r.copy_ids.length)), r.content_status === "truncated" && /*#__PURE__*/React.createElement("span", {
      className: "lp-tag"
    }, "\u0442\u0435\u043A\u0441\u0442 \u043E\u0431\u0440\u0435\u0437\u0430\u043D"), (r.content_status === "fetch_failed" || r.content_status === "empty") && /*#__PURE__*/React.createElement("span", {
      className: "lp-tag lp-tag-warn"
    }, "\u0442\u0435\u043A\u0441\u0442 \u043D\u0435 \u0437\u0430\u0433\u0440\u0443\u0436\u0435\u043D")));
  };

  // Стрелки вверх/вниз переводят фокус и выбор по заголовкам списка.
  const onListKeys = e => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const titles = [...e.currentTarget.querySelectorAll(".lp-c-title")];
    const i = titles.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    const next = titles[Math.max(0, Math.min(titles.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
    next.focus();
    next.click();
  };
  const notConfirmedHidden = fClassification === "confirmed" && types ? types.not_confirmed : 0;
  const pickedVisible = records.filter(r => selected.has(r.record_id)).length;
  const catalogList = /*#__PURE__*/React.createElement("div", {
    className: "lp-list"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-lhead"
  }, /*#__PURE__*/React.createElement("span", {
    className: "lp-lcount"
  }, fmtInt(recordsTotal), " ", recordWord(recordsTotal)), seg("Сортировка", fSort, [["new", "сначала новые"], ["conf", "по вероятности"]], setFSort), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-btn-sm",
    "aria-pressed": selectMode,
    onClick: () => {
      setSelectMode(m => !m);
      if (selectMode) setSelected(new Set());
    }
  }, selectMode ? "Готово" : "Выбрать"), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-btn-sm",
    onClick: exportExcel,
    disabled: loading || recordsTotal === 0
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "dl",
    size: 14
  }), "Excel", selectMode && selected.size ? ` · ${selected.size}` : "")), selectMode && /*#__PURE__*/React.createElement("div", {
    className: "lp-selbar"
  }, /*#__PURE__*/React.createElement("span", null, selected.size ? `Выбрано ${fmtInt(selected.size)}` : "Отметьте записи для выгрузки"), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn-text",
    onClick: () => setSelected(pickedVisible === records.length && records.length ? new Set() : new Set(records.map(r => r.record_id)))
  }, pickedVisible === records.length && records.length ? "Снять все" : "Выбрать все показанные")), notConfirmedHidden > 0 && /*#__PURE__*/React.createElement("div", {
    className: "lp-hid"
  }, /*#__PURE__*/React.createElement("span", null, "\u0415\u0449\u0451 ", fmtInt(notConfirmedHidden), " ", lpPlural(notConfirmedHidden, "запись", "записи", "записей"), " \u0431\u0435\u0437 \u043D\u0430\u0445\u043E\u0434\u043A\u0438 \u0441\u043A\u0440\u044B\u0442\u044B: \u043C\u043E\u0434\u0435\u043B\u044C \u043D\u0435 \u043F\u043E\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u043B\u0430 \u0443\u044F\u0437\u0432\u0438\u043C\u043E\u0441\u0442\u044C \u0438\u043B\u0438 \u0441\u0445\u0435\u043C\u0443."), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn-text",
    onClick: () => setFClassification("all")
  }, "\u041F\u043E\u043A\u0430\u0437\u0430\u0442\u044C \u0432\u0441\u0435")), /*#__PURE__*/React.createElement("div", {
    role: "list",
    "aria-label": "\u0417\u0430\u043F\u0438\u0441\u0438 \u0431\u0430\u0437\u044B",
    onKeyDown: onListKeys
  }, records.map(listItem)), records.length < recordsTotal && /*#__PURE__*/React.createElement("div", {
    className: "lp-more"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn",
    disabled: loading,
    onClick: () => setPage(p => p + 1)
  }, loading ? "Загружаем…" : `Показать ещё ${fmtInt(Math.min(PAGE_SIZE, recordsTotal - records.length))}`), /*#__PURE__*/React.createElement("span", {
    className: "lp-muted"
  }, "\u041F\u043E\u043A\u0430\u0437\u0430\u043D\u043E ", fmtInt(records.length), " \u0438\u0437 ", fmtInt(recordsTotal))));
  const catalogPanel = /*#__PURE__*/React.createElement("section", {
    className: "lp-panel",
    id: "lp-panel-catalog",
    role: "tabpanel",
    "aria-labelledby": "lp-tab-catalog"
  }, kpis, /*#__PURE__*/React.createElement("section", {
    className: "lp-card lp-base"
  }, filters, loading && !records.length ? /*#__PURE__*/React.createElement("div", {
    className: "lp-skeleton-list"
  }, [0, 1, 2, 3, 4].map(i => /*#__PURE__*/React.createElement("div", {
    key: i,
    className: "lp-sk-row"
  }, /*#__PURE__*/React.createElement("span", {
    className: "lp-sk lp-sk-30"
  }), /*#__PURE__*/React.createElement("span", {
    className: "lp-sk lp-sk-80"
  }), /*#__PURE__*/React.createElement("span", {
    className: "lp-sk lp-sk-60"
  })))) : recordsError ? /*#__PURE__*/React.createElement("div", {
    className: "lp-state",
    role: "alert"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-state-ic lp-state-ic-err"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "alert",
    size: 20
  })), /*#__PURE__*/React.createElement("p", {
    className: "lp-state-t"
  }, "\u041D\u0435 \u0443\u0434\u0430\u043B\u043E\u0441\u044C \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044C \u0437\u0430\u043F\u0438\u0441\u0438"), /*#__PURE__*/React.createElement("p", {
    className: "lp-state-x"
  }, "\u041F\u0440\u043E\u0432\u0435\u0440\u044C\u0442\u0435 \u0441\u043E\u0435\u0434\u0438\u043D\u0435\u043D\u0438\u0435 \u0438 \u043F\u043E\u0432\u0442\u043E\u0440\u0438\u0442\u0435. \u0414\u0430\u043D\u043D\u044B\u0435 \u043D\u0430 \u043C\u0435\u0441\u0442\u0435."), /*#__PURE__*/React.createElement("div", {
    className: "lp-state-a"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-btn-primary",
    onClick: loadRecords
  }, "\u041F\u043E\u0432\u0442\u043E\u0440\u0438\u0442\u044C"))) : !records.length ? /*#__PURE__*/React.createElement("div", {
    className: "lp-state"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-state-ic"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "search",
    size: 20
  })), /*#__PURE__*/React.createElement("p", {
    className: "lp-state-t"
  }, "\u041D\u0438\u0447\u0435\u0433\u043E \u043D\u0435 \u043D\u0430\u0448\u043B\u043E\u0441\u044C"), /*#__PURE__*/React.createElement("p", {
    className: "lp-state-x"
  }, fClassification === "confirmed" && notConfirmedHidden ? "Среди уязвимостей и схем совпадений нет, но есть записи без находки. Покажите все или начните исследование по этой теме." : "Под выбранные фильтры не подходит ни одна запись. Уберите часть фильтров или начните исследование по этой теме."), /*#__PURE__*/React.createElement("div", {
    className: "lp-state-a"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn",
    onClick: resetAll
  }, "\u0421\u0431\u0440\u043E\u0441\u0438\u0442\u044C \u0444\u0438\u043B\u044C\u0442\u0440\u044B"), fClassification === "confirmed" && notConfirmedHidden > 0 && /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn",
    onClick: () => setFClassification("all")
  }, "\u041F\u043E\u043A\u0430\u0437\u0430\u0442\u044C \u0432\u0441\u0435"), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-btn-primary",
    onClick: () => openContext("ai_research")
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "spark"
  }), "\u0418\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u0442\u044C"))) : /*#__PURE__*/React.createElement("div", {
    className: "lp-split" + (readerOpen ? " lp-split-open" : "")
  }, catalogList, /*#__PURE__*/React.createElement("div", {
    className: "lp-rd-wrap",
    ref: readerRef
  }, selRecord && /*#__PURE__*/React.createElement("article", {
    className: "lp-rd",
    "aria-label": "\u0417\u0430\u043F\u0438\u0441\u044C"
  }, readerHead(selRecord, records, "base"), recordBody(selRecord, "base"))))));

  // ── Очередь ────────────────────────────────────────────────────────────────
  const decisionPanel = r => {
    const d = draftOf(r.record_id);
    const ok = !!(d.cls && (d.comment || "").trim());
    const modelKind = recordKind(r);
    const options = [["vulnerability", "Уязвимость", "1"], ["fraud_scheme", "Мошенническая схема", "2"], ["not_confirmed", "Не подтверждено", "3"]];
    return /*#__PURE__*/React.createElement("div", {
      className: "lp-dec"
    }, /*#__PURE__*/React.createElement("div", {
      className: "lp-dec-t"
    }, "\u0420\u0435\u0448\u0435\u043D\u0438\u0435 \u044D\u043A\u0441\u043F\u0435\u0440\u0442\u0430", /*#__PURE__*/React.createElement("span", null, "\u043E\u0434\u043D\u043E \u043D\u0430 \u0437\u0430\u043F\u0438\u0441\u044C, \u043F\u043E\u043F\u0430\u0434\u0451\u0442 \u0432 \u0438\u0441\u0442\u043E\u0440\u0438\u044E")), /*#__PURE__*/React.createElement("div", {
      className: "lp-dopts",
      role: "radiogroup",
      "aria-label": "\u0420\u0435\u0448\u0435\u043D\u0438\u0435"
    }, options.map(([v, label, key]) => /*#__PURE__*/React.createElement("button", {
      key: v,
      type: "button",
      role: "radio",
      "aria-checked": d.cls === v,
      className: "lp-dopt" + (d.cls === v ? " lp-dopt-on" : ""),
      onClick: () => {
        setDraft(r.record_id, {
          cls: v
        });
        if (commentRef.current) commentRef.current.focus();
      }
    }, /*#__PURE__*/React.createElement("span", {
      className: "lp-sw lp-sw-" + KIND_LABELS[v][1]
    }), /*#__PURE__*/React.createElement("span", null, label, modelKind === v && /*#__PURE__*/React.createElement("small", null, "\u0442\u0430\u043A \u0441\u0447\u0438\u0442\u0430\u0435\u0442 \u043C\u043E\u0434\u0435\u043B\u044C")), /*#__PURE__*/React.createElement("span", {
      className: "lp-kbd"
    }, key)))), queueCopies(r).length > 0 && /*#__PURE__*/React.createElement("label", {
      className: "lp-dcopies",
      htmlFor: "lp-apply-copies"
    }, /*#__PURE__*/React.createElement("input", {
      id: "lp-apply-copies",
      type: "checkbox",
      checked: d.applyCopies !== false,
      onChange: e => setDraft(r.record_id, {
        applyCopies: e.target.checked
      })
    }), "\u041F\u0440\u0438\u043C\u0435\u043D\u0438\u0442\u044C \u0438 \u043A ", fmtInt(queueCopies(r).length), " ", lpPlural(queueCopies(r).length, "копии", "копиям", "копиям"), " \u0432 \u043E\u0447\u0435\u0440\u0435\u0434\u0438", /*#__PURE__*/React.createElement("span", {
      className: "lp-muted"
    }, " \u2014 \u0442\u043E\u0442 \u0436\u0435 \u0444\u0440\u0430\u0433\u043C\u0435\u043D\u0442: \u2116", queueCopies(r).join(", №"))), /*#__PURE__*/React.createElement("label", {
      className: "lp-sr-only",
      htmlFor: "lp-queue-comment-input"
    }, "\u041A\u043E\u043C\u043C\u0435\u043D\u0442\u0430\u0440\u0438\u0439 \u0443\u0447\u0430\u0441\u0442\u043D\u0438\u043A\u0430 \u0426\u041A"), /*#__PURE__*/React.createElement("textarea", {
      id: "lp-queue-comment-input",
      ref: commentRef,
      className: "lp-dcom",
      rows: 3,
      value: d.comment,
      onChange: e => setDraft(r.record_id, {
        comment: e.target.value
      }),
      placeholder: "\u041F\u043E\u0447\u0435\u043C\u0443 \u0442\u0430\u043A\u043E\u0435 \u0440\u0435\u0448\u0435\u043D\u0438\u0435? \u041A\u043E\u043C\u043C\u0435\u043D\u0442\u0430\u0440\u0438\u0439 \u043E\u0431\u044F\u0437\u0430\u0442\u0435\u043B\u0435\u043D: \u0435\u0433\u043E \u0443\u0432\u0438\u0434\u044F\u0442 \u0430\u0443\u0434\u0438\u0442\u043E\u0440\u044B \u0432 \u0438\u0441\u0442\u043E\u0440\u0438\u0438 \u0437\u0430\u043F\u0438\u0441\u0438."
    }), /*#__PURE__*/React.createElement("div", {
      className: "lp-dec-f"
    }, /*#__PURE__*/React.createElement("span", {
      className: "lp-dhint"
    }, !d.cls ? "Выберите решение" : !ok ? "Добавьте комментарий: без него решение не сохранить" : "Готово к сохранению"), /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-btn lp-btn-primary",
      disabled: !ok || markBusy,
      onClick: saveDecision
    }, "\u0421\u043E\u0445\u0440\u0430\u043D\u0438\u0442\u044C \u0440\u0435\u0448\u0435\u043D\u0438\u0435")), /*#__PURE__*/React.createElement("div", {
      className: "lp-kh"
    }, /*#__PURE__*/React.createElement("span", null, /*#__PURE__*/React.createElement("span", {
      className: "lp-kbd"
    }, "J"), /*#__PURE__*/React.createElement("span", {
      className: "lp-kbd"
    }, "K"), "\u043F\u043E \u043E\u0447\u0435\u0440\u0435\u0434\u0438"), /*#__PURE__*/React.createElement("span", null, /*#__PURE__*/React.createElement("span", {
      className: "lp-kbd"
    }, "1"), /*#__PURE__*/React.createElement("span", {
      className: "lp-kbd"
    }, "2"), /*#__PURE__*/React.createElement("span", {
      className: "lp-kbd"
    }, "3"), "\u0440\u0435\u0448\u0435\u043D\u0438\u0435"), /*#__PURE__*/React.createElement("span", null, /*#__PURE__*/React.createElement("span", {
      className: "lp-kbd"
    }, "\u2318"), /*#__PURE__*/React.createElement("span", {
      className: "lp-kbd"
    }, "Enter"), "\u0441\u043E\u0445\u0440\u0430\u043D\u0438\u0442\u044C")));
  };
  const queuePanel = /*#__PURE__*/React.createElement("section", {
    className: "lp-panel",
    id: "lp-panel-queue",
    role: "tabpanel",
    "aria-labelledby": "lp-tab-queue"
  }, queueDenied ? /*#__PURE__*/React.createElement("section", {
    className: "lp-card"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-state"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-state-ic"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "shield",
    size: 20
  })), /*#__PURE__*/React.createElement("h2", {
    className: "lp-state-t"
  }, "\u041D\u0435\u0442 \u0434\u043E\u0441\u0442\u0443\u043F\u0430 \u043A \u043E\u0447\u0435\u0440\u0435\u0434\u0438 \u0432\u0435\u0440\u0438\u0444\u0438\u043A\u0430\u0446\u0438\u0438"), /*#__PURE__*/React.createElement("p", {
    className: "lp-state-x"
  }, "\u0420\u043E\u043B\u044C \u044D\u043A\u0441\u043F\u0435\u0440\u0442\u0430 \u0426\u041A \u041A\u0421 \u043D\u0435 \u043D\u0430\u0437\u043D\u0430\u0447\u0435\u043D\u0430 \u0438\u043B\u0438 \u043E\u0442\u043E\u0437\u0432\u0430\u043D\u0430."), /*#__PURE__*/React.createElement("div", {
    className: "lp-state-a"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn",
    onClick: () => setView("catalog")
  }, "\u0412\u0435\u0440\u043D\u0443\u0442\u044C\u0441\u044F \u043A \u0431\u0430\u0437\u0435")))) : queueLoading && !queueRecords.length ? /*#__PURE__*/React.createElement("section", {
    className: "lp-card"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-skeleton-list"
  }, [0, 1, 2].map(i => /*#__PURE__*/React.createElement("div", {
    key: i,
    className: "lp-sk-row"
  }, /*#__PURE__*/React.createElement("span", {
    className: "lp-sk lp-sk-80"
  }), /*#__PURE__*/React.createElement("span", {
    className: "lp-sk lp-sk-60"
  }))))) : queueError ? /*#__PURE__*/React.createElement("section", {
    className: "lp-card"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-state",
    role: "alert"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-state-ic lp-state-ic-err"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "alert",
    size: 20
  })), /*#__PURE__*/React.createElement("p", {
    className: "lp-state-t"
  }, "\u041D\u0435 \u0443\u0434\u0430\u043B\u043E\u0441\u044C \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044C \u043E\u0447\u0435\u0440\u0435\u0434\u044C \u0432\u0435\u0440\u0438\u0444\u0438\u043A\u0430\u0446\u0438\u0438"), /*#__PURE__*/React.createElement("div", {
    className: "lp-state-a"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-btn-primary",
    onClick: loadQueue
  }, "\u041F\u043E\u0432\u0442\u043E\u0440\u0438\u0442\u044C")))) : !queueList.length ? /*#__PURE__*/React.createElement("section", {
    className: "lp-card"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-state"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-state-ic lp-state-ic-ok"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "check",
    size: 20
  })), /*#__PURE__*/React.createElement("p", {
    className: "lp-state-t"
  }, "\u041E\u0447\u0435\u0440\u0435\u0434\u044C \u0440\u0430\u0437\u043E\u0431\u0440\u0430\u043D\u0430"), /*#__PURE__*/React.createElement("p", {
    className: "lp-state-x"
  }, "\u041D\u043E\u0432\u044B\u0435 \u0437\u0430\u043F\u0438\u0441\u0438 \u043F\u043E\u044F\u0432\u044F\u0442\u0441\u044F \u043F\u043E\u0441\u043B\u0435 \u0441\u0431\u043E\u0440\u0430 \u0438\u0441\u0442\u043E\u0447\u043D\u0438\u043A\u043E\u0432 \u0438 \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u0439 \u0430\u0433\u0435\u043D\u0442\u0430."), /*#__PURE__*/React.createElement("div", {
    className: "lp-state-a"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn",
    onClick: () => setView("catalog")
  }, "\u041E\u0442\u043A\u0440\u044B\u0442\u044C \u0431\u0430\u0437\u0443")))) : /*#__PURE__*/React.createElement("div", {
    className: "lp-qsplit" + (qOpen ? " lp-qsplit-open" : "")
  }, /*#__PURE__*/React.createElement("section", {
    className: "lp-card lp-qlist",
    "aria-label": "\u0417\u0430\u043F\u0438\u0441\u0438 \u043D\u0430 \u043F\u0440\u043E\u0432\u0435\u0440\u043A\u0443"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-qhead"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-qh1"
  }, /*#__PURE__*/React.createElement("b", null, "\u0416\u0434\u0443\u0442 \u0440\u0435\u0448\u0435\u043D\u0438\u044F"), /*#__PURE__*/React.createElement("span", {
    className: "lp-tnum"
  }, fmtInt(queueCount))), seg("Порядок", qSort, [["old", "сначала старые"], ["conf", "по вероятности"]], pickQueueSort)), queueList.map(r => {
    const active = qSel && qSel.record_id === r.record_id;
    const meta = [knownBank(r.bank_slug), r.collected_at ? `собрано ${fmtDay(r.collected_at)}` : null].filter(Boolean).join(" · ");
    const copiesHere = queueCopies(r).length;
    return /*#__PURE__*/React.createElement("button", {
      key: r.record_id,
      id: `lp-qi-${r.record_id}`,
      type: "button",
      className: "lp-qi" + (active ? " lp-qi-on" : ""),
      "aria-current": active ? "true" : undefined,
      onClick: () => {
        setQueueSelectedId(r.record_id);
        setQOpen(true);
        showReader(qCardRef);
      }
    }, /*#__PURE__*/React.createElement("span", {
      className: "lp-qi-t"
    }, r.headline || r.title || r.snippet || "Без заголовка"), /*#__PURE__*/React.createElement("span", {
      className: "lp-qi-m"
    }, meta || "банк и дата не указаны", /*#__PURE__*/React.createElement(KindBadge, {
      kind: recordKind(r)
    }), pctOf(r.verdict_confidence) != null && /*#__PURE__*/React.createElement("span", {
      className: "lp-tnum"
    }, pctOf(r.verdict_confidence), "%"), r.summary_doubt && /*#__PURE__*/React.createElement("span", {
      className: "lp-doubt"
    }, "\u0441\u043E\u043C\u043D\u0435\u043D\u0438\u0435"), copiesHere > 0 && /*#__PURE__*/React.createElement("span", null, "+", fmtInt(copiesHere), " ", lpPlural(copiesHere, "копия", "копии", "копий"))));
  })), qSel && /*#__PURE__*/React.createElement("section", {
    className: "lp-card lp-qcard",
    ref: qCardRef,
    "aria-label": "\u041A\u0430\u0440\u0442\u043E\u0447\u043A\u0430 \u043F\u0440\u043E\u0432\u0435\u0440\u043A\u0438",
    "aria-live": "polite"
  }, readerHead(qSel, queueList, "queue"), recordBody(qSel, "queue"), canMarkVerdict ? decisionPanel(qSel) : /*#__PURE__*/React.createElement("div", {
    className: "lp-dec"
  }, /*#__PURE__*/React.createElement("p", {
    className: "lp-dhint"
  }, "\u0420\u0435\u0448\u0435\u043D\u0438\u0435 \u0432\u044B\u043D\u043E\u0441\u0438\u0442 \u044D\u043A\u0441\u043F\u0435\u0440\u0442 \u0426\u041A \u041A\u0421.")))));

  // ── Исследование ───────────────────────────────────────────────────────────
  const SUGGESTIONS = ["Схемы с оплатой по QR-коду в СБП", "Как обходят лимиты на снятие наличных", "Лазейки в бонусах за приглашение друзей", "Уязвимости в кэшбэке за оплату ЖКУ"];
  const toolCount = (name, failed = false) => toolEvents.filter(e => e.name === name && (failed ? e.kind === "result" && e.status === "failed" : e.kind === "call")).length;
  const searchCalls = toolCount("audit_web_search");
  const fetchCalls = toolCount("audit_web_fetch");
  const fetchFailed = toolCount("audit_web_fetch", true);
  const agentsBusy = subagents.some(a => ["queued", "searching", "classifying"].includes(a.status));
  const materialsTotal = subagents.reduce((s, a) => s + (a.total || 0), 0);
  const materialsDone = subagents.reduce((s, a) => s + (a.completed || 0), 0);
  // Поток оборвался или вернул ошибку после начала работы: карточка остаётся и
  // показывает, докуда дошло исследование, — иначе обрыв выглядит как пустота.
  const stopped = !chatLoading && ["execute", "answer", "error"].includes(phase) && (toolEvents.length > 0 || subagents.length > 0);
  const phaseRank = {
    clarify: 0,
    await_clarify: 0,
    execute: 1,
    answer: 2,
    done: 3
  }[phase] ?? -1;
  const baseStepState = i => {
    if (phase === "done") return "done";
    if (i === 0) return phaseRank >= 1 || stopped ? "done" : phaseRank === 0 ? "run" : "wait";
    if (phaseRank < 1 && !stopped) return "wait";
    if (i === 4) return phase === "answer" ? "run" : "wait";
    if (phaseRank >= 2) return "done";
    if (i === 1) return fetchCalls > 0 || subagents.length ? "done" : "run";
    if (i === 2) return fetchCalls > 0 ? subagents.length ? "done" : "run" : "wait";
    if (i === 3) return subagents.length ? agentsBusy || subagents.some(a => a.status === "cancelled") ? "run" : "done" : "wait";
    return "wait";
  };
  const stepState = i => {
    const st = baseStepState(i);
    return stopped && st === "run" ? "stop" : st;
  };
  const plural = (n, one, few, many) => `${fmtInt(n)} ${lpPlural(n, one, few, many)}`;
  const steps = [["Уточнение запроса", ""], ["Поиск источников", searchCalls ? plural(searchCalls, "запрос", "запроса", "запросов") : ""], ["Чтение страниц", fetchCalls ? plural(fetchCalls, "страница", "страницы", "страниц") + (fetchFailed ? ` · не открылось: ${fmtInt(fetchFailed)}` : "") : ""], ["Разметка материалов", subagents.length ? plural(subagents.length, "исследователь", "исследователя", "исследователей") + (materialsTotal ? ` · ${fmtInt(materialsDone)} из ${fmtInt(materialsTotal)}` : "") : ""], ["Итог", phase === "done" ? "готово" : ""]];
  const elapsed = researchActivity && Number.isInteger(researchActivity.elapsed) ? researchActivity.elapsed : 0;
  const fmtElapsed = sec => sec < 60 ? `${sec} с` : `${Math.floor(sec / 60)} мин ${sec % 60} с`;
  const stepsCard = chatLoading || stopped ? /*#__PURE__*/React.createElement("div", {
    className: "lp-steps" + (stopped ? " lp-steps-stopped" : ""),
    "aria-live": "polite"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-steps-h"
  }, stopped ? /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement(Icon, {
    name: "alert"
  }), /*#__PURE__*/React.createElement("b", null, "\u0418\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u0435 \u043F\u0440\u0435\u0440\u0432\u0430\u043D\u043E"), /*#__PURE__*/React.createElement("span", {
    className: "lp-steps-m"
  }, "\u0448\u0430\u0433\u0438 \u043D\u0438\u0436\u0435 \u2014 \u0434\u043E\u043A\u0443\u0434\u0430 \u0434\u043E\u0448\u043B\u043E; \u0437\u0430\u043F\u0440\u043E\u0441 \u043C\u043E\u0436\u043D\u043E \u043E\u0442\u043F\u0440\u0430\u0432\u0438\u0442\u044C \u0435\u0449\u0451 \u0440\u0430\u0437")) : /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("b", null, "\u0418\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u0435 \u0438\u0434\u0451\u0442"), researchActivity && researchActivity.message && /*#__PURE__*/React.createElement("span", {
    className: "lp-steps-m",
    role: "status",
    "aria-label": "\u0422\u0435\u043A\u0443\u0449\u0438\u0439 \u044D\u0442\u0430\u043F \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u044F"
  }, researchActivity.message, " \xB7 ", /*#__PURE__*/React.createElement("span", {
    className: "lp-tnum"
  }, fmtElapsed(elapsed))))), !stopped && /*#__PURE__*/React.createElement("div", {
    className: "lp-pbar"
  }, /*#__PURE__*/React.createElement("i", {
    style: {
      width: `${researchProgress}%`
    }
  })), steps.map(([title, count], i) => {
    const st = stepState(i);
    return /*#__PURE__*/React.createElement("div", {
      key: title,
      className: "lp-step lp-step-" + st
    }, /*#__PURE__*/React.createElement("span", {
      className: "lp-si"
    }, st === "done" && /*#__PURE__*/React.createElement(Icon, {
      name: "check",
      size: 11
    })), title, /*#__PURE__*/React.createElement("span", {
      className: "lp-sc"
    }, st === "stop" ? [count, "прервано"].filter(Boolean).join(" · ") : count));
  }), subagents.length > 0 && /*#__PURE__*/React.createElement("details", {
    className: "lp-agents",
    open: agentsOpen,
    onToggle: e => setAgentsOpen(e.currentTarget.open)
  }, /*#__PURE__*/React.createElement("summary", null, "\u041F\u043E\u0434\u0440\u043E\u0431\u043D\u043E\u0441\u0442\u0438 \u043F\u043E \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u0442\u0435\u043B\u044F\u043C"), /*#__PURE__*/React.createElement(SubagentCards, {
    agents: subagents
  }))) : phase === "done" && (searchCalls || fetchCalls || subagents.length) ? /*#__PURE__*/React.createElement("div", {
    className: "lp-steps lp-steps-small"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-steps-h"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "check"
  }), /*#__PURE__*/React.createElement("b", null, "\u0413\u043E\u0442\u043E\u0432\u043E"), /*#__PURE__*/React.createElement("span", {
    className: "lp-steps-m"
  }, [searchCalls && plural(searchCalls, "поисковый запрос", "поисковых запроса", "поисковых запросов"), fetchCalls && `прочитано ${plural(fetchCalls, "страница", "страницы", "страниц")}`, subagents.length && plural(subagents.length, "исследователь", "исследователя", "исследователей")].filter(Boolean).join(" · ")))) : null;
  const clarifyCard = !researchReadOnly && selectionQuestions.length > 0 && /*#__PURE__*/React.createElement("div", {
    className: "lp-msg-a"
  }, /*#__PURE__*/React.createElement("span", {
    className: "lp-ag"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "spark",
    size: 14
  })), /*#__PURE__*/React.createElement("div", {
    className: "lp-msg-body"
  }, /*#__PURE__*/React.createElement("p", null, "\u0423\u0442\u043E\u0447\u043D\u044E, \u0447\u0442\u043E\u0431\u044B \u043D\u0435 \u0447\u0438\u0442\u0430\u0442\u044C \u043B\u0438\u0448\u043D\u0435\u0433\u043E."), /*#__PURE__*/React.createElement("div", {
    className: "lp-clar"
  }, selectionQuestions.map(q => {
    const answer = answersByQ[q.id] || {
      selected: [],
      other: ""
    };
    const multi = q.type === "multi";
    return /*#__PURE__*/React.createElement("div", {
      className: "lp-question",
      key: q.id || q.question
    }, /*#__PURE__*/React.createElement("p", {
      className: "lp-clar-q"
    }, q.question), /*#__PURE__*/React.createElement("div", {
      className: "lp-opts",
      role: multi ? "group" : "radiogroup",
      "aria-label": q.question
    }, (q.options || []).map((opt, i) => {
      const checked = answer.selected.includes(opt.value);
      const optionInputId = `lp-question-${q.id}-${i}`;
      return /*#__PURE__*/React.createElement("label", {
        key: opt.value || i,
        htmlFor: optionInputId,
        className: "lp-opt" + (checked ? " lp-opt-on" : "")
      }, /*#__PURE__*/React.createElement("input", {
        id: optionInputId,
        type: multi ? "checkbox" : "radio",
        name: "q-" + q.id,
        checked: checked,
        onChange: () => toggleAnswer(q.id, opt.value, multi)
      }), checked && /*#__PURE__*/React.createElement(Icon, {
        name: "check",
        size: 13
      }), opt.label || opt.value, opt.recommended ? /*#__PURE__*/React.createElement("span", {
        className: "lp-opt-rec"
      }, "\u0440\u0435\u043A\u043E\u043C\u0435\u043D\u0434\u0443\u0435\u043C") : null);
    })), q.allow_other && /*#__PURE__*/React.createElement("div", {
      className: "lp-question-other"
    }, /*#__PURE__*/React.createElement("label", {
      htmlFor: `lp-question-other-${q.id}`
    }, "\u0421\u0432\u043E\u0439 \u0432\u0430\u0440\u0438\u0430\u043D\u0442"), /*#__PURE__*/React.createElement("textarea", {
      id: `lp-question-other-${q.id}`,
      rows: 2,
      value: answer.other || "",
      onChange: e => setOtherText(q.id, e.target.value),
      placeholder: "\u041E\u043F\u0438\u0448\u0438\u0442\u0435 \u0438\u043D\u0430\u0447\u0435\u2026"
    })));
  }), !selectionAnswersComplete && /*#__PURE__*/React.createElement("p", {
    className: "lp-note"
  }, "\u041E\u0442\u0432\u0435\u0442\u044C\u0442\u0435 \u043D\u0430 \u0432\u0441\u0435 \u0432\u043E\u043F\u0440\u043E\u0441\u044B \u043F\u0435\u0440\u0435\u0434 \u0437\u0430\u043F\u0443\u0441\u043A\u043E\u043C."), /*#__PURE__*/React.createElement("div", {
    className: "lp-acts"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-btn-primary",
    disabled: clarifySubmitting || !selectionAnswersComplete,
    onClick: submitAnswers
  }, clarifySubmitting ? "Запускаем…" : "Начать исследование")))));
  const reportActions = message => !researchReadOnly && message.report_id ? /*#__PURE__*/React.createElement("div", {
    className: "lp-acts lp-msg-acts"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-btn-sm",
    disabled: agentBusy || researchActionBusy,
    onClick: () => downloadResearchReport(message.report_id, "pdf")
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "doc",
    size: 14
  }), "PDF"), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-btn-sm",
    disabled: agentBusy || researchActionBusy || researchLoading,
    onClick: shareResearch
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "share",
    size: 14
  }), "\u041F\u043E\u0434\u0435\u043B\u0438\u0442\u044C\u0441\u044F")) : null;
  const conversation = chat.map((m, i) => {
    if (m.role === "user") {
      return /*#__PURE__*/React.createElement("div", {
        key: i,
        className: "lp-msg-u"
      }, m.content, researchReadOnly && /*#__PURE__*/React.createElement("small", null, "\u0410\u0432\u0442\u043E\u0440 \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u044F"));
    }
    const text = String(m.content || "");
    // Пока ответа нет, ход работы показывает карточка шагов — пустой пузырь не нужен.
    if (!text.trim()) return null;
    return /*#__PURE__*/React.createElement("div", {
      key: i,
      className: "lp-msg-a" + (m._live ? " lp-msg-live" : "")
    }, /*#__PURE__*/React.createElement("span", {
      className: "lp-ag"
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "spark",
      size: 14
    })), /*#__PURE__*/React.createElement("div", {
      className: "lp-msg-body"
    }, /*#__PURE__*/React.createElement(SafeMarkdown, {
      content: text
    }), reportActions(m)));
  });
  const findingsBlock = findings.length > 0 && /*#__PURE__*/React.createElement("div", {
    className: "lp-cands"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-cands-h"
  }, /*#__PURE__*/React.createElement("b", null, "\u041D\u0430\u0445\u043E\u0434\u043A\u0438 \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u044F \xB7 ", fmtInt(findings.length)), /*#__PURE__*/React.createElement("span", null, "\u0434\u043E\u0431\u0430\u0432\u043B\u0435\u043D\u044B \u0432 \u043E\u0431\u0449\u0443\u044E \u0431\u0430\u0437\u0443", canQueue ? " и ждут решения в очереди" : ", ждут проверки экспертом")), findings.map(f => {
    const kind = recordKind(f);
    const manual = f.verdict_model === "manual";
    return /*#__PURE__*/React.createElement("button", {
      key: f.record_id,
      type: "button",
      className: "lp-cand",
      onClick: () => openRecordInBase(f)
    }, /*#__PURE__*/React.createElement("span", {
      className: "lp-cand-top"
    }, /*#__PURE__*/React.createElement(KindBadge, {
      kind: kind
    }), manual || f.reviewed ? /*#__PURE__*/React.createElement("span", {
      className: "lp-done"
    }, "\u043F\u0440\u043E\u0432\u0435\u0440\u0435\u043D\u043E") : f.awaiting ? /*#__PURE__*/React.createElement("span", {
      className: "lp-pend"
    }, "\u0436\u0434\u0451\u0442 \u043F\u0440\u043E\u0432\u0435\u0440\u043A\u0438") : null, !manual && pctOf(f.verdict_confidence) != null && /*#__PURE__*/React.createElement("span", {
      className: "lp-muted"
    }, "\u0432\u0435\u0440\u043E\u044F\u0442\u043D\u043E\u0441\u0442\u044C ", pctOf(f.verdict_confidence), "%")), /*#__PURE__*/React.createElement("span", {
      className: "lp-cand-t"
    }, f.headline || f.title || f.snippet || "Без заголовка"), (f.summary || f.verdict_reason) && /*#__PURE__*/React.createElement("span", {
      className: "lp-cand-d"
    }, f.summary || f.verdict_reason), /*#__PURE__*/React.createElement("span", {
      className: "lp-cand-m"
    }, /*#__PURE__*/React.createElement("span", {
      className: bankClass(f.bank_slug)
    }, bankName(f.bank_slug)), /*#__PURE__*/React.createElement("span", null, [f.domain || hostOf(f.url), fmtDay(f.published_at || f.collected_at)].filter(Boolean).join(" · ")), /*#__PURE__*/React.createElement("span", {
      className: "lp-link"
    }, "\u043E\u0442\u043A\u0440\u044B\u0442\u044C \u0432 \u0431\u0430\u0437\u0435", /*#__PURE__*/React.createElement(Icon, {
      name: "right",
      size: 12
    }))));
  }));
  const earlyReports = savedReports.filter(rep => !chat.some(m => m.report_id === rep.report_id));
  const earlyReportsBlock = earlyReports.length > 0 && /*#__PURE__*/React.createElement("section", {
    className: "lp-reports",
    "aria-label": "\u0420\u0430\u043D\u043D\u0438\u0435 \u043E\u0442\u0447\u0451\u0442\u044B"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-reports-h"
  }, /*#__PURE__*/React.createElement("b", null, "\u0420\u0430\u043D\u043D\u0438\u0435 \u043E\u0442\u0447\u0451\u0442\u044B \xB7 ", fmtInt(earlyReports.length)), /*#__PURE__*/React.createElement("span", null, "\u0441\u043E\u0445\u0440\u0430\u043D\u0435\u043D\u044B \u0440\u0430\u043D\u044C\u0448\u0435, \u0447\u0435\u043C \u043E\u0442\u0432\u0435\u0442\u044B \u0441\u0442\u0430\u043B\u0438 \u0445\u0440\u0430\u043D\u0438\u0442\u044C\u0441\u044F \u0432 \u043F\u0435\u0440\u0435\u043F\u0438\u0441\u043A\u0435")), earlyReports.map(rep => /*#__PURE__*/React.createElement("details", {
    key: rep.report_id,
    className: "lp-report"
  }, /*#__PURE__*/React.createElement("summary", null, /*#__PURE__*/React.createElement(Icon, {
    name: "right",
    size: 14
  }), /*#__PURE__*/React.createElement("span", {
    className: "lp-report-q"
  }, rep.query || "Отчёт исследования"), rep.created_at && /*#__PURE__*/React.createElement("time", {
    dateTime: rep.created_at
  }, fmtDate(rep.created_at))), /*#__PURE__*/React.createElement("div", {
    className: "lp-report-b"
  }, /*#__PURE__*/React.createElement(SafeMarkdown, {
    content: String(rep.result || "")
  }), /*#__PURE__*/React.createElement("div", {
    className: "lp-acts"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-btn-sm",
    disabled: agentBusy || researchActionBusy,
    onClick: () => downloadResearchReport(rep.report_id, "pdf")
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "doc",
    size: 14
  }), "PDF"))))));
  const welcome = /*#__PURE__*/React.createElement("div", {
    className: "lp-welcome"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-eyebrow"
  }, "\u041D\u043E\u0432\u043E\u0435 \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u0435"), /*#__PURE__*/React.createElement("h2", null, "\u0427\u0442\u043E \u043F\u0440\u043E\u0432\u0435\u0440\u0438\u0442\u044C?"), /*#__PURE__*/React.createElement("p", null, "\u0410\u0433\u0435\u043D\u0442 \u0438\u0449\u0435\u0442 \u043E\u0431\u0441\u0443\u0436\u0434\u0435\u043D\u0438\u044F \u043D\u0430 \u0444\u043E\u0440\u0443\u043C\u0430\u0445 \u0438 \u0441\u0430\u0439\u0442\u0430\u0445 \u0431\u0430\u043D\u043A\u043E\u0432, \u0447\u0438\u0442\u0430\u0435\u0442 \u043D\u0430\u0439\u0434\u0435\u043D\u043D\u044B\u0435 \u0441\u0442\u0440\u0430\u043D\u0438\u0446\u044B \u0438 \u0440\u0430\u0437\u043C\u0435\u0447\u0430\u0435\u0442 \u043D\u0430\u0445\u043E\u0434\u043A\u0438: \u0443\u044F\u0437\u0432\u0438\u043C\u043E\u0441\u0442\u044C, \u043C\u043E\u0448\u0435\u043D\u043D\u0438\u0447\u0435\u0441\u043A\u0430\u044F \u0441\u0445\u0435\u043C\u0430 \u0438\u043B\u0438 \u043D\u0438 \u0442\u043E \u043D\u0438 \u0434\u0440\u0443\u0433\u043E\u0435. \u041D\u0430\u0445\u043E\u0434\u043A\u0438 \u043F\u043E\u043F\u0430\u0434\u0430\u044E\u0442 \u0432 \u043E\u0431\u0449\u0443\u044E \u0431\u0430\u0437\u0443 \u0438 \u0436\u0434\u0443\u0442 \u0440\u0435\u0448\u0435\u043D\u0438\u044F \u044D\u043A\u0441\u043F\u0435\u0440\u0442\u0430 \u0426\u041A \u041A\u0421."), /*#__PURE__*/React.createElement("div", {
    className: "lp-sugg"
  }, SUGGESTIONS.map(s => /*#__PURE__*/React.createElement("button", {
    key: s,
    type: "button",
    className: "lp-sg",
    disabled: agentBusy || researchLoading || researchReadOnly || !workspaceId,
    onClick: () => sendChat(s)
  }, s))), /*#__PURE__*/React.createElement("div", {
    className: "lp-wfacts"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-wf"
  }, /*#__PURE__*/React.createElement("b", null, "\u0423\u0442\u043E\u0447\u043D\u0438\u0442 \u0437\u0430\u043F\u0440\u043E\u0441"), /*#__PURE__*/React.createElement("span", null, "\u0411\u0430\u043D\u043A\u0438 \u0438 \u043F\u0435\u0440\u0438\u043E\u0434 \u0441\u043F\u0440\u043E\u0441\u0438\u0442 \u043A\u043D\u043E\u043F\u043A\u0430\u043C\u0438, \u0447\u0442\u043E\u0431\u044B \u043D\u0435 \u0447\u0438\u0442\u0430\u0442\u044C \u043B\u0438\u0448\u043D\u0435\u0433\u043E.")), /*#__PURE__*/React.createElement("div", {
    className: "lp-wf"
  }, /*#__PURE__*/React.createElement("b", null, "\u041F\u043E\u043A\u0430\u0436\u0435\u0442 \u0445\u043E\u0434 \u0440\u0430\u0431\u043E\u0442\u044B"), /*#__PURE__*/React.createElement("span", null, "\u0421\u043A\u043E\u043B\u044C\u043A\u043E \u0437\u0430\u043F\u0440\u043E\u0441\u043E\u0432, \u0441\u0442\u0440\u0430\u043D\u0438\u0446 \u0438 \u043C\u0430\u0442\u0435\u0440\u0438\u0430\u043B\u043E\u0432 \u0443\u0436\u0435 \u043E\u0431\u0440\u0430\u0431\u043E\u0442\u0430\u043D\u043E.")), /*#__PURE__*/React.createElement("div", {
    className: "lp-wf"
  }, /*#__PURE__*/React.createElement("b", null, "\u0421\u043B\u043E\u0436\u0438\u0442 \u043D\u0430\u0445\u043E\u0434\u043A\u0438 \u0432 \u0431\u0430\u0437\u0443"), /*#__PURE__*/React.createElement("span", null, "\u042D\u043A\u0441\u043F\u0435\u0440\u0442 \u0426\u041A \u041A\u0421 \u0443\u0432\u0438\u0434\u0438\u0442 \u0438\u0445 \u0432 \u043E\u0447\u0435\u0440\u0435\u0434\u0438 \u0438 \u0432\u044B\u043D\u0435\u0441\u0435\u0442 \u0440\u0435\u0448\u0435\u043D\u0438\u0435."))));
  const composer = !researchReadOnly && /*#__PURE__*/React.createElement("div", {
    className: "lp-composer"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-cmp"
  }, /*#__PURE__*/React.createElement("label", {
    className: "lp-sr-only",
    htmlFor: "lp-chat-input"
  }, "\u0421\u043E\u043E\u0431\u0449\u0435\u043D\u0438\u0435 \u0430\u043D\u0430\u043B\u0438\u0442\u0438\u043A\u0443"), /*#__PURE__*/React.createElement("textarea", {
    id: "lp-chat-input",
    ref: chatInputRef,
    rows: 1,
    value: chatInput,
    onChange: e => {
      setChatInput(e.target.value);
      if (clarifyError) setClarifyError("");
      e.target.style.height = "auto";
      e.target.style.height = Math.min(160, e.target.scrollHeight) + "px";
    },
    onKeyDown: e => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        if (textClarification && chatInput.trim()) submitAnswers();else if (!currentQuestions.length && chatInput.trim()) sendChat();
      }
    },
    placeholder: agentBusy ? "Идёт исследование. Дождитесь итога." : textClarification ? "Ответ на уточняющий вопрос…" : selectionQuestions.length ? "Сначала ответьте на уточняющие вопросы…" : chat.length ? "Уточните или спросите дальше" : "Опишите, что искать: продукт, банк, признаки схемы",
    disabled: agentBusy || researchLoading || researchActionBusy || !workspaceId || selectionQuestions.length > 0
  }), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-send",
    "aria-label": "\u041E\u0442\u043F\u0440\u0430\u0432\u0438\u0442\u044C \u0441\u043E\u043E\u0431\u0449\u0435\u043D\u0438\u0435",
    onClick: () => textClarification ? submitAnswers() : sendChat(),
    disabled: agentBusy || researchLoading || researchActionBusy || !workspaceId || !chatInput.trim() || selectionQuestions.length > 0
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "send"
  }))), /*#__PURE__*/React.createElement("p", {
    className: "lp-cmp-note"
  }, "Enter \u2014 \u043E\u0442\u043F\u0440\u0430\u0432\u0438\u0442\u044C \xB7 Shift+Enter \u2014 \u043D\u043E\u0432\u0430\u044F \u0441\u0442\u0440\u043E\u043A\u0430"));
  const historyAside = /*#__PURE__*/React.createElement("aside", {
    className: "lp-card lp-hist",
    "aria-labelledby": "lp-research-history-title"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-hist-new",
    disabled: agentBusy || researchActionBusy || researchLoading,
    onClick: () => {
      setHistOpen(false);
      createResearch();
    }
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "plus"
  }), "\u041D\u043E\u0432\u043E\u0435 \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u0435"), /*#__PURE__*/React.createElement("h2", {
    id: "lp-research-history-title",
    className: "lp-eyebrow"
  }, "\u041C\u043E\u0438 \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u044F"), historyListLoading && /*#__PURE__*/React.createElement("p", {
    className: "lp-muted",
    role: "status"
  }, "\u0417\u0430\u0433\u0440\u0443\u0437\u043A\u0430 \u0441\u043F\u0438\u0441\u043A\u0430\u2026"), historyListError && /*#__PURE__*/React.createElement("div", {
    className: "lp-inline-err",
    role: "alert"
  }, /*#__PURE__*/React.createElement("p", null, historyListError), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-btn-sm",
    disabled: historyListLoading || agentBusy || researchActionBusy,
    onClick: () => workspaceId ? loadResearchList() : initializeResearch()
  }, "\u041F\u043E\u0432\u0442\u043E\u0440\u0438\u0442\u044C \u0437\u0430\u0433\u0440\u0443\u0437\u043A\u0443 \u0438\u0441\u0442\u043E\u0440\u0438\u0438")), !historyListLoading && !historyListError && !researches.length && /*#__PURE__*/React.createElement("p", {
    className: "lp-muted"
  }, "\u0418\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u0439 \u043F\u043E\u043A\u0430 \u043D\u0435\u0442."), /*#__PURE__*/React.createElement("div", {
    className: "lp-hist-list"
  }, researches.filter(item => item.has_messages !== false || item.workspace_id === workspaceId).map(item => {
    const fullName = String(item.name || "Без названия").trim() || "Без названия";
    const active = workspaceId === item.workspace_id && !researchReadOnly;
    return /*#__PURE__*/React.createElement("div", {
      key: item.workspace_id,
      className: "lp-hi" + (active ? " lp-hi-on" : "")
    }, /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-hi-open",
      "aria-label": `Открыть исследование ${fullName}`,
      "aria-current": active ? "true" : undefined,
      disabled: agentBusy || researchActionBusy,
      onClick: () => {
        setHistOpen(false);
        openResearch({
          id: item.workspace_id
        });
      }
    }, /*#__PURE__*/React.createElement("b", {
      title: fullName
    }, researchListName(fullName)), /*#__PURE__*/React.createElement("time", {
      dateTime: item.last_active_at || item.created_at || undefined
    }, fmtDate(item.last_active_at || item.created_at))), /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-hi-del",
      "aria-label": `Удалить исследование ${fullName} из истории`,
      title: "\u0423\u0434\u0430\u043B\u0438\u0442\u044C \u0438\u0437 \u0438\u0441\u0442\u043E\u0440\u0438\u0438",
      disabled: agentBusy || researchActionBusy || researchLoading,
      onClick: () => requestResearchDelete(item)
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "x",
      size: 14
    })));
  })));
  const researchPanel = /*#__PURE__*/React.createElement("section", {
    className: "lp-panel",
    id: "lp-panel-ai_research",
    role: "tabpanel",
    "aria-labelledby": "lp-tab-ai_research"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-rs" + (histOpen ? " lp-rs-hist" : "")
  }, historyAside, /*#__PURE__*/React.createElement("section", {
    className: "lp-card lp-rs-main",
    "aria-label": "\u0425\u043E\u0434 AI-\u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u044F"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-stream",
    ref: chatScrollRef
  }, researchReadOnly && !researchLoading && researchWorkspace && /*#__PURE__*/React.createElement("div", {
    className: "lp-callout"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "info"
  }), /*#__PURE__*/React.createElement("span", null, "\u0418\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u0435 \u0434\u043E\u0441\u0442\u0443\u043F\u043D\u043E \u0442\u043E\u043B\u044C\u043A\u043E \u0434\u043B\u044F \u0447\u0442\u0435\u043D\u0438\u044F.")), researchError && /*#__PURE__*/React.createElement("div", {
    className: "lp-inline-err",
    role: "alert"
  }, /*#__PURE__*/React.createElement("p", null, researchError), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-btn-sm",
    disabled: agentBusy || researchActionBusy || researchLoading,
    onClick: () => researchTargetRef.current.create ? createResearch(researchTargetRef.current.showResearch) : researchTargetRef.current.id || researchTargetRef.current.token ? openResearch(researchTargetRef.current) : initializeResearch()
  }, "\u041F\u043E\u0432\u0442\u043E\u0440\u0438\u0442\u044C \u0437\u0430\u0433\u0440\u0443\u0437\u043A\u0443 \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u044F")), researchLoading ? /*#__PURE__*/React.createElement("p", {
    className: "lp-muted",
    role: "status"
  }, "\u0417\u0430\u0433\u0440\u0443\u0437\u043A\u0430 \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u044F\u2026") : /*#__PURE__*/React.createElement(React.Fragment, null, researchWorkspace && chat.length > 0 && /*#__PURE__*/React.createElement("div", {
    className: "lp-rs-head"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-eyebrow"
  }, "\u0418\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u0435 \xB7 ", fmtDate(researchWorkspace.last_active_at || researchWorkspace.created_at)), /*#__PURE__*/React.createElement("h2", null, researchWorkspace.name || "Исследование")), chat.length === 0 && !researchReadOnly && !earlyReports.length ? welcome : conversation, earlyReportsBlock, clarifyCard, clarifySubmitting && !chatLoading && /*#__PURE__*/React.createElement("div", {
    className: "lp-steps lp-steps-small",
    role: "status",
    "aria-label": "\u041F\u043E\u0434\u0433\u043E\u0442\u043E\u0432\u043A\u0430 \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u044F"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-steps-h"
  }, /*#__PURE__*/React.createElement("span", {
    className: "lp-si lp-si-run"
  }), /*#__PURE__*/React.createElement("b", null, "\u0413\u043E\u0442\u043E\u0432\u0438\u043C \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u0435"), /*#__PURE__*/React.createElement("span", {
    className: "lp-steps-m"
  }, "\u0443\u0442\u043E\u0447\u043D\u0435\u043D\u0438\u0435 \u043F\u0440\u0438\u043D\u044F\u0442\u043E"))), stepsCard, findingsBlock, clarifyError && /*#__PURE__*/React.createElement("div", {
    className: "lp-inline-err",
    role: "alert"
  }, /*#__PURE__*/React.createElement("p", null, clarifyError)), researchShareUrl && /*#__PURE__*/React.createElement("div", {
    className: "lp-share"
  }, /*#__PURE__*/React.createElement("label", {
    htmlFor: "lp-research-share-url"
  }, "\u0421\u0441\u044B\u043B\u043A\u0430 \u043D\u0430 \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u0435"), /*#__PURE__*/React.createElement("input", {
    id: "lp-research-share-url",
    value: researchShareUrl,
    readOnly: true,
    onFocus: e => e.target.select()
  }), /*#__PURE__*/React.createElement("p", {
    className: "lp-note"
  }, "\u041F\u043E\u043B\u0443\u0447\u0430\u0442\u0435\u043B\u044E \u043F\u043E\u0442\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044F \u0432\u0445\u043E\u0434 \u0432 \u043C\u043E\u0434\u0443\u043B\u044C. \u041F\u0440\u043E\u0441\u043C\u043E\u0442\u0440 \u0434\u043E\u0441\u0442\u0443\u043F\u0435\u043D \u0431\u0435\u0437 \u043F\u0440\u0430\u0432\u0430 \u0440\u0435\u0434\u0430\u043A\u0442\u0438\u0440\u043E\u0432\u0430\u043D\u0438\u044F.")))), composer)));

  // ── Всплывающие панели ─────────────────────────────────────────────────────
  const popLayer = pop && (() => {
    const width = pop.kind === "method" ? 340 : 300;
    const left = Math.max(8, Math.min(pop.kind === "case" ? pop.rect.right - width : pop.rect.left, window.innerWidth - width - 8));
    const style = {
      left,
      top: pop.rect.bottom + 6,
      width
    };
    if (pop.kind === "banks") {
      // Банки текущего среза (со счётчиками); пока сводки нет — справочник.
      // Коды-синонимы одного банка (sber / sberbank) показываются одной строкой.
      const byName = new Map();
      for (const slug of (bankFacet.length ? bankFacet.map(b => b.slug) : bankOptions).filter(slug => knownBank(slug))) {
        const name = bankName(slug);
        if (!byName.has(name) || bankCount(slug) > bankCount(byName.get(name))) byName.set(name, slug);
      }
      const slugs = [...new Set([...byName.values(), ...fBanks])];
      slugs.sort((a, b) => bankCount(b) - bankCount(a) || bankName(a).localeCompare(bankName(b)));
      return /*#__PURE__*/React.createElement("div", {
        className: "lp-pop lp-pop-banks",
        style: style,
        role: "dialog",
        "aria-label": "\u0411\u0430\u043D\u043A\u0438"
      }, /*#__PURE__*/React.createElement("div", {
        className: "lp-pop-list"
      }, slugs.length === 0 && /*#__PURE__*/React.createElement("p", {
        className: "lp-pop-p"
      }, "\u0411\u0430\u043D\u043A\u0438 \u0432 \u0437\u0430\u043F\u0438\u0441\u044F\u0445 \u043D\u0435 \u0443\u043A\u0430\u0437\u0430\u043D\u044B."), slugs.map(b => /*#__PURE__*/React.createElement("label", {
        key: b,
        className: "lp-pi",
        htmlFor: `lp-bank-${b}`
      }, /*#__PURE__*/React.createElement("input", {
        id: `lp-bank-${b}`,
        type: "checkbox",
        checked: fBanks.includes(b),
        onChange: () => setFBanks(prev => prev.includes(b) ? prev.filter(x => x !== b) : [...prev, b])
      }), /*#__PURE__*/React.createElement("span", {
        className: bankClass(b)
      }, bankName(b)), /*#__PURE__*/React.createElement("span", {
        className: "lp-pi-n"
      }, fmtInt(bankCount(b)))))), /*#__PURE__*/React.createElement("div", {
        className: "lp-pop-f"
      }, /*#__PURE__*/React.createElement("button", {
        type: "button",
        className: "lp-btn-text",
        onClick: () => setFBanks([])
      }, "\u0412\u0441\u0435 \u0431\u0430\u043D\u043A\u0438"), /*#__PURE__*/React.createElement("button", {
        type: "button",
        className: "lp-btn lp-btn-primary lp-btn-sm",
        onClick: () => setPop(null)
      }, "\u0413\u043E\u0442\u043E\u0432\u043E")));
    }
    if (pop.kind === "case") {
      return /*#__PURE__*/React.createElement("div", {
        className: "lp-pop",
        style: style,
        role: "dialog",
        "aria-label": "\u0410\u0443\u0434\u0438\u0442-\u0434\u0435\u043B\u0430"
      }, /*#__PURE__*/React.createElement("div", {
        className: "lp-pop-h lp-eyebrow"
      }, "\u0414\u043E\u0431\u0430\u0432\u0438\u0442\u044C \u0432 \u0430\u0443\u0434\u0438\u0442-\u0434\u0435\u043B\u043E"), cases === null ? /*#__PURE__*/React.createElement("p", {
        className: "lp-pop-p"
      }, "\u0417\u0430\u0433\u0440\u0443\u0436\u0430\u0435\u043C \u0434\u0435\u043B\u0430\u2026") : cases.length === 0 ? /*#__PURE__*/React.createElement("p", {
        className: "lp-pop-p"
      }, "\u0414\u0435\u043B \u043F\u043E\u043A\u0430 \u043D\u0435\u0442 \u2014 \u0441\u043E\u0437\u0434\u0430\u0439\u0442\u0435 \u043F\u0435\u0440\u0432\u043E\u0435.") : /*#__PURE__*/React.createElement("div", {
        className: "lp-pop-list"
      }, cases.map(c => /*#__PURE__*/React.createElement("button", {
        key: c.case_id,
        type: "button",
        className: "lp-pi",
        onClick: () => addToCase(c, pop.record)
      }, /*#__PURE__*/React.createElement(Icon, {
        name: "case",
        size: 14
      }), /*#__PURE__*/React.createElement("span", null, c.title), /*#__PURE__*/React.createElement("span", {
        className: "lp-pi-n"
      }, fmtInt(c.items || 0))))), /*#__PURE__*/React.createElement("form", {
        className: "lp-pop-f lp-pop-new",
        onSubmit: e => {
          e.preventDefault();
          createCaseWith(pop.record);
        }
      }, /*#__PURE__*/React.createElement("label", {
        className: "lp-sr-only",
        htmlFor: "lp-new-case"
      }, "\u041D\u0430\u0437\u0432\u0430\u043D\u0438\u0435 \u043D\u043E\u0432\u043E\u0433\u043E \u0434\u0435\u043B\u0430"), /*#__PURE__*/React.createElement("input", {
        id: "lp-new-case",
        value: newCaseTitle,
        onChange: e => setNewCaseTitle(e.target.value),
        placeholder: "\u041D\u043E\u0432\u043E\u0435 \u0434\u0435\u043B\u043E"
      }), /*#__PURE__*/React.createElement("button", {
        type: "submit",
        className: "lp-btn lp-btn-sm",
        disabled: !newCaseTitle.trim()
      }, "\u0421\u043E\u0437\u0434\u0430\u0442\u044C")));
    }
    return /*#__PURE__*/React.createElement("div", {
      className: "lp-pop",
      style: style,
      role: "dialog",
      "aria-label": "\u041A\u0430\u043A \u0447\u0438\u0442\u0430\u0442\u044C \u0432\u0435\u0440\u0434\u0438\u043A\u0442"
    }, /*#__PURE__*/React.createElement("p", {
      className: "lp-pop-p"
    }, /*#__PURE__*/React.createElement("b", null, "\u0412\u0435\u0440\u0434\u0438\u043A\u0442"), " \u0441\u043D\u0430\u0447\u0430\u043B\u0430 \u0441\u0442\u0430\u0432\u0438\u0442 \u043C\u043E\u0434\u0435\u043B\u044C \u043F\u0440\u0438 \u0441\u0431\u043E\u0440\u0435 \u0437\u0430\u043F\u0438\u0441\u0438. \u041E\u043A\u043E\u043D\u0447\u0430\u0442\u0435\u043B\u044C\u043D\u044B\u0439 \u0432\u044B\u043D\u043E\u0441\u0438\u0442 \u044D\u043A\u0441\u043F\u0435\u0440\u0442 \u0426\u041A \u041A\u0421; \u043F\u043E\u043A\u0430 \u043E\u043D \u043D\u0435 \u0440\u0435\u0448\u0438\u043B, \u0437\u0430\u043F\u0438\u0441\u044C \u043F\u043E\u043C\u0435\u0447\u0435\u043D\u0430 \xAB\u0436\u0434\u0451\u0442 \u043F\u0440\u043E\u0432\u0435\u0440\u043A\u0438\xBB."), /*#__PURE__*/React.createElement("p", {
      className: "lp-pop-p"
    }, /*#__PURE__*/React.createElement("b", null, "\u0412\u0435\u0440\u043E\u044F\u0442\u043D\u043E\u0441\u0442\u044C"), " \u2014 \u043D\u0430\u0441\u043A\u043E\u043B\u044C\u043A\u043E \u0437\u0430\u043F\u0438\u0441\u044C \u043F\u043E\u0445\u043E\u0436\u0430 \u043D\u0430 \u0443\u044F\u0437\u0432\u0438\u043C\u043E\u0441\u0442\u044C \u0438\u043B\u0438 \u0441\u0445\u0435\u043C\u0443 \u043F\u043E \u043E\u0446\u0435\u043D\u043A\u0435 \u043C\u043E\u0434\u0435\u043B\u0438. \u042D\u0442\u043E \u043D\u0435 \u0432\u0435\u0440\u043E\u044F\u0442\u043D\u043E\u0441\u0442\u044C \u0443\u0449\u0435\u0440\u0431\u0430."), /*#__PURE__*/React.createElement("p", {
      className: "lp-pop-p"
    }, /*#__PURE__*/React.createElement("b", null, "\u0421\u0443\u0442\u044C"), " \u043C\u043E\u0434\u0435\u043B\u044C \u0441\u043E\u0441\u0442\u0430\u0432\u043B\u044F\u0435\u0442 \u0442\u043E\u043B\u044C\u043A\u043E \u0434\u043B\u044F \u0443\u044F\u0437\u0432\u0438\u043C\u043E\u0441\u0442\u0435\u0439 \u0438 \u0441\u0445\u0435\u043C. \u0423 \u0437\u0430\u043F\u0438\u0441\u0435\u0439 \u0431\u0435\u0437 \u043D\u0430\u0445\u043E\u0434\u043A\u0438 \u043E\u0441\u0442\u0430\u0451\u0442\u0441\u044F \u043A\u043E\u0440\u043E\u0442\u043A\u0438\u0439 \u043A\u043E\u043C\u043C\u0435\u043D\u0442\u0430\u0440\u0438\u0439 \u043A\u043B\u0430\u0441\u0441\u0438\u0444\u0438\u043A\u0430\u0442\u043E\u0440\u0430 \u0438\u0437 \u0442\u043E\u0433\u043E \u0436\u0435 \u0432\u044B\u0437\u043E\u0432\u0430, \u0447\u0442\u043E \u0438 \u0432\u0435\u0440\u0434\u0438\u043A\u0442."));
  })();

  // ── Доступ (администратор) ─────────────────────────────────────────────────
  const experts = adminRoles ? adminRoles.roles.filter(a => a.status === "active" && !hiddenExperts.has(a.username)) : [];
  const AUDIT_LABELS_RU = AUDIT_ACTION_LABELS;
  const accessSheet = accessOpen && /*#__PURE__*/React.createElement("div", {
    className: "lp-layer"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-scrim",
    "aria-label": "\u0417\u0430\u043A\u0440\u044B\u0442\u044C \u043F\u0430\u043D\u0435\u043B\u044C \u0434\u043E\u0441\u0442\u0443\u043F\u0430",
    tabIndex: -1,
    onClick: () => setAccessOpen(false)
  }), /*#__PURE__*/React.createElement("div", {
    className: "lp-sheet",
    ref: accessSheetRef,
    role: "dialog",
    "aria-modal": "true",
    "aria-labelledby": "lp-access-title"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-sh-h"
  }, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("div", {
    className: "lp-eyebrow"
  }, "\u0410\u0434\u043C\u0438\u043D\u0438\u0441\u0442\u0440\u0438\u0440\u043E\u0432\u0430\u043D\u0438\u0435"), /*#__PURE__*/React.createElement("h2", {
    id: "lp-access-title"
  }, "\u0414\u043E\u0441\u0442\u0443\u043F \u043A \u043C\u043E\u0434\u0443\u043B\u044E")), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-icb",
    "aria-label": "\u0417\u0430\u043A\u0440\u044B\u0442\u044C",
    onClick: () => setAccessOpen(false)
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "x"
  }))), /*#__PURE__*/React.createElement("div", {
    className: "lp-sh-b"
  }, adminDenied ? /*#__PURE__*/React.createElement("div", {
    className: "lp-state"
  }, /*#__PURE__*/React.createElement("h2", {
    className: "lp-state-t"
  }, "\u041D\u0435\u0442 \u0434\u043E\u0441\u0442\u0443\u043F\u0430 \u043A \u0430\u0434\u043C\u0438\u043D\u0438\u0441\u0442\u0440\u0438\u0440\u043E\u0432\u0430\u043D\u0438\u044E"), /*#__PURE__*/React.createElement("p", {
    className: "lp-state-x"
  }, "\u0420\u043E\u043B\u044C \u0430\u0434\u043C\u0438\u043D\u0438\u0441\u0442\u0440\u0430\u0442\u043E\u0440\u0430 \u043C\u043E\u0434\u0443\u043B\u044F \u043D\u0435 \u043D\u0430\u0437\u043D\u0430\u0447\u0435\u043D\u0430 \u0438\u043B\u0438 \u043E\u0442\u043E\u0437\u0432\u0430\u043D\u0430.")) : adminError ? /*#__PURE__*/React.createElement("div", {
    className: "lp-state",
    role: "alert"
  }, /*#__PURE__*/React.createElement("p", {
    className: "lp-state-t"
  }, "\u041D\u0435 \u0443\u0434\u0430\u043B\u043E\u0441\u044C \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044C \u0434\u0430\u043D\u043D\u044B\u0435 \u0430\u0434\u043C\u0438\u043D\u0438\u0441\u0442\u0440\u0438\u0440\u043E\u0432\u0430\u043D\u0438\u044F"), /*#__PURE__*/React.createElement("div", {
    className: "lp-state-a"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-btn-primary",
    onClick: loadAdmin
  }, "\u041F\u043E\u0432\u0442\u043E\u0440\u0438\u0442\u044C"))) : adminLoading && !adminRoles ? /*#__PURE__*/React.createElement("p", {
    className: "lp-muted"
  }, "\u0417\u0430\u0433\u0440\u0443\u0436\u0430\u0435\u043C\u2026") : /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("section", null, /*#__PURE__*/React.createElement("h3", {
    className: "lp-eyebrow"
  }, "\u042D\u043A\u0441\u043F\u0435\u0440\u0442\u044B \u0426\u041A \u041A\u0421 \xB7 ", adminRoles ? adminRoles.active_experts - hiddenExperts.size : "…", " \u0438\u0437 ", adminRoles ? adminRoles.max_experts : 5), experts.length === 0 && /*#__PURE__*/React.createElement("p", {
    className: "lp-muted"
  }, "\u041D\u0430\u0437\u043D\u0430\u0447\u0435\u043D\u0438\u0439 \u0440\u043E\u043B\u0438 \u0426\u041A \u041A\u0421 \u043D\u0435\u0442."), experts.map(a => /*#__PURE__*/React.createElement("div", {
    key: a.username,
    className: "lp-row-l"
  }, /*#__PURE__*/React.createElement("span", {
    className: "lp-ava"
  }, String(a.username || "?")[0].toUpperCase()), /*#__PURE__*/React.createElement("span", {
    className: "lp-grow"
  }, /*#__PURE__*/React.createElement("b", null, a.username), /*#__PURE__*/React.createElement("span", {
    className: "lp-sub"
  }, "\u043D\u0430\u0437\u043D\u0430\u0447\u0435\u043D ", fmtDate(a.created_at))), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-btn-sm",
    disabled: adminBusy,
    onClick: () => revokeLater(a.username)
  }, "\u041E\u0442\u043E\u0437\u0432\u0430\u0442\u044C"))), /*#__PURE__*/React.createElement("form", {
    className: "lp-grant",
    onSubmit: e => {
      e.preventDefault();
      grantRole();
    }
  }, /*#__PURE__*/React.createElement("label", {
    htmlFor: "lp-grant-name"
  }, "\u041B\u043E\u0433\u0438\u043D \u0441\u043E\u0442\u0440\u0443\u0434\u043D\u0438\u043A\u0430"), /*#__PURE__*/React.createElement("input", {
    id: "lp-grant-name",
    value: grantName,
    onChange: e => setGrantName(e.target.value),
    placeholder: "ivanova.a",
    autoComplete: "off",
    disabled: adminRoles && adminRoles.active_experts >= adminRoles.max_experts
  }), /*#__PURE__*/React.createElement("span", {
    className: "lp-note"
  }, adminRoles && adminRoles.active_experts >= adminRoles.max_experts ? "Достигнут предел экспертов. Чтобы назначить нового, отзовите одного." : "Эксперт увидит очередь проверки и сможет выносить решения."), /*#__PURE__*/React.createElement("button", {
    type: "submit",
    className: "lp-btn lp-btn-primary",
    disabled: adminBusy || !grantName.trim()
  }, "\u041D\u0430\u0437\u043D\u0430\u0447\u0438\u0442\u044C \u044D\u043A\u0441\u043F\u0435\u0440\u0442\u043E\u043C"))), /*#__PURE__*/React.createElement("section", null, /*#__PURE__*/React.createElement("h3", {
    className: "lp-eyebrow"
  }, "\u0416\u0443\u0440\u043D\u0430\u043B \u0434\u043E\u0441\u0442\u0443\u043F\u0430"), /*#__PURE__*/React.createElement("p", {
    className: "lp-note"
  }, "\u041E\u0431\u0435\u0437\u043B\u0438\u0447\u0435\u043D\u043D\u0430\u044F \u0441\u0432\u043E\u0434\u043A\u0430 \u0441\u043E\u0431\u044B\u0442\u0438\u0439 \u0430\u0432\u0442\u043E\u0440\u0438\u0437\u0430\u0446\u0438\u0438 \u0438 \u0438\u0437\u043C\u0435\u043D\u0435\u043D\u0438\u0439 \u0440\u043E\u043B\u0435\u0439."), !adminAudit || adminAudit.length === 0 ? /*#__PURE__*/React.createElement("p", {
    className: "lp-muted"
  }, "\u0421\u043E\u0431\u044B\u0442\u0438\u0439 \u043F\u043E\u043A\u0430 \u043D\u0435\u0442.") : adminAudit.map(e => /*#__PURE__*/React.createElement("div", {
    key: e.action + ":" + e.decision,
    className: "lp-jr"
  }, /*#__PURE__*/React.createElement("span", null, AUDIT_LABELS_RU[e.action] || e.action), /*#__PURE__*/React.createElement("span", {
    className: "lp-tnum"
  }, fmtInt(e.count)), /*#__PURE__*/React.createElement("span", {
    className: "lp-sub"
  }, /*#__PURE__*/React.createElement("span", {
    className: e.decision === "deny" ? "lp-warn-t" : ""
  }, AUDIT_DECISION_LABELS[e.decision] || e.decision), " \xB7 \u043F\u043E\u0441\u043B\u0435\u0434\u043D\u0435\u0435 ", fmtDate(e.last_at)))))))));

  // ── Диалог «Изменить вердикт» (база, у проверенных записей) ───────────────
  const verdictDialog = canMarkVerdict && verdictModal && (() => {
    const rec = {
      ...verdictModal.record,
      ...(detailOf(verdictModal.record.record_id) || {})
    };
    const current = recordKind(rec);
    const cls = verdictModal.cls || null;
    const comment = markComment.trim();
    const unchanged = cls === current && rec.verdict_model === "manual";
    const ready = !!cls && !!comment && !unchanged;
    const save = async () => {
      if (!ready) return;
      const ok = await markVerdict([rec.record_id], cls, comment, {
        source: "base"
      });
      if (ok) {
        setVerdictModal(null);
        setMarkComment("");
      }
    };
    return /*#__PURE__*/React.createElement("div", {
      className: "lp-layer"
    }, /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-scrim",
      "aria-label": "\u0417\u0430\u043A\u0440\u044B\u0442\u044C \u0434\u0438\u0430\u043B\u043E\u0433",
      tabIndex: -1,
      onClick: () => setVerdictModal(null)
    }), /*#__PURE__*/React.createElement("div", {
      className: "lp-dialog",
      ref: verdictDialogRef,
      role: "dialog",
      "aria-modal": "true",
      "aria-labelledby": "lp-verdict-title"
    }, /*#__PURE__*/React.createElement("div", {
      className: "lp-sh-h"
    }, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("div", {
      className: "lp-eyebrow"
    }, "\u0420\u0435\u0448\u0435\u043D\u0438\u0435 \u044D\u043A\u0441\u043F\u0435\u0440\u0442\u0430"), /*#__PURE__*/React.createElement("h2", {
      id: "lp-verdict-title"
    }, "\u0418\u0437\u043C\u0435\u043D\u0438\u0442\u044C \u0432\u0435\u0440\u0434\u0438\u043A\u0442")), /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-icb",
      "aria-label": "\u0417\u0430\u043A\u0440\u044B\u0442\u044C",
      onClick: () => setVerdictModal(null)
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "x"
    }))), /*#__PURE__*/React.createElement("div", {
      className: "lp-dlg-b"
    }, /*#__PURE__*/React.createElement("p", {
      className: "lp-dlg-rec"
    }, rec.title || rec.snippet || "Без заголовка"), /*#__PURE__*/React.createElement("div", {
      className: "lp-dopts",
      role: "radiogroup",
      "aria-label": "\u0412\u0435\u0440\u0434\u0438\u043A\u0442"
    }, ["vulnerability", "fraud_scheme", "not_confirmed"].map(v => /*#__PURE__*/React.createElement("button", {
      key: v,
      type: "button",
      role: "radio",
      "aria-checked": cls === v,
      className: "lp-dopt" + (cls === v ? " lp-dopt-on" : ""),
      onClick: () => setVerdictModal(m => ({
        ...m,
        cls: v
      }))
    }, /*#__PURE__*/React.createElement("span", {
      className: "lp-sw lp-sw-" + KIND_LABELS[v][1]
    }), /*#__PURE__*/React.createElement("span", null, KIND_LABELS[v][0], current === v && /*#__PURE__*/React.createElement("small", null, "\u0441\u0435\u0439\u0447\u0430\u0441"))))), /*#__PURE__*/React.createElement("label", {
      className: "lp-sr-only",
      htmlFor: "lp-mark-comment"
    }, "\u041A\u043E\u043C\u043C\u0435\u043D\u0442\u0430\u0440\u0438\u0439 \u044D\u043A\u0441\u043F\u0435\u0440\u0442\u0430"), /*#__PURE__*/React.createElement("textarea", {
      id: "lp-mark-comment",
      className: "lp-dcom",
      rows: 3,
      value: markComment,
      onChange: e => setMarkComment(e.target.value),
      placeholder: "\u041F\u043E\u0447\u0435\u043C\u0443 \u0432\u0435\u0440\u0434\u0438\u043A\u0442 \u043C\u0435\u043D\u044F\u0435\u0442\u0441\u044F? \u041A\u043E\u043C\u043C\u0435\u043D\u0442\u0430\u0440\u0438\u0439 \u043F\u043E\u043F\u0430\u0434\u0451\u0442 \u0432 \u0438\u0441\u0442\u043E\u0440\u0438\u044E \u0437\u0430\u043F\u0438\u0441\u0438."
    }), /*#__PURE__*/React.createElement("div", {
      className: "lp-dec-f"
    }, /*#__PURE__*/React.createElement("span", {
      className: "lp-dhint"
    }, !cls ? "Выберите вердикт" : unchanged ? "Вердикт уже такой" : !comment ? "Добавьте комментарий" : "Готово к сохранению"), /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-btn",
      onClick: () => setVerdictModal(null)
    }, "\u041E\u0442\u043C\u0435\u043D\u0430"), /*#__PURE__*/React.createElement("button", {
      type: "button",
      className: "lp-btn lp-btn-primary",
      disabled: !ready || markBusy,
      onClick: save
    }, markBusy ? "Сохраняем…" : "Сохранить")))));
  })();

  // ── Удаление исследования из личной истории ───────────────────────────────
  const researchDeleteDialog = researchDeleteConfirm && /*#__PURE__*/React.createElement("div", {
    className: "lp-layer"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-scrim",
    "aria-label": "\u0417\u0430\u043A\u0440\u044B\u0442\u044C \u043F\u043E\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043D\u0438\u0435 \u0443\u0434\u0430\u043B\u0435\u043D\u0438\u044F",
    tabIndex: -1,
    disabled: researchActionBusy,
    onClick: () => {
      setResearchDeleteConfirm(false);
      setResearchDeleteTarget(null);
    }
  }), /*#__PURE__*/React.createElement("div", {
    className: "lp-dialog lp-dialog-sm",
    ref: researchDeleteDialogRef,
    role: "dialog",
    "aria-modal": "true",
    "aria-labelledby": "lp-research-delete-title"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-sh-h"
  }, /*#__PURE__*/React.createElement("h2", {
    id: "lp-research-delete-title"
  }, "\u0423\u0434\u0430\u043B\u0438\u0442\u044C \u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u0435 \u0438\u0437 \u0438\u0441\u0442\u043E\u0440\u0438\u0438?")), /*#__PURE__*/React.createElement("div", {
    className: "lp-dlg-b"
  }, /*#__PURE__*/React.createElement("p", {
    className: "lp-dlg-p"
  }, "\u0418\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u0435 \u0438\u0441\u0447\u0435\u0437\u043D\u0435\u0442 \u0438\u0437 \u043B\u0438\u0447\u043D\u043E\u0439 \u0438\u0441\u0442\u043E\u0440\u0438\u0438, \u0430 \u043E\u0431\u0449\u0430\u044F \u0441\u0441\u044B\u043B\u043A\u0430 \u043F\u0435\u0440\u0435\u0441\u0442\u0430\u043D\u0435\u0442 \u0440\u0430\u0431\u043E\u0442\u0430\u0442\u044C. \u0414\u0430\u043D\u043D\u044B\u0435 \u0441\u043E\u0445\u0440\u0430\u043D\u044F\u0442\u0441\u044F \u0432 \u0441\u0438\u0441\u0442\u0435\u043C\u0435, \u043D\u0430\u0445\u043E\u0434\u043A\u0438 \u043E\u0441\u0442\u0430\u043D\u0443\u0442\u0441\u044F \u0432 \u043E\u0431\u0449\u0435\u0439 \u0431\u0430\u0437\u0435."), researchDeleteError && /*#__PURE__*/React.createElement("p", {
    role: "alert",
    className: "lp-warn-t"
  }, researchDeleteError), /*#__PURE__*/React.createElement("div", {
    className: "lp-dec-f"
  }, /*#__PURE__*/React.createElement("button", {
    ref: researchDeleteCancelRef,
    type: "button",
    className: "lp-btn",
    disabled: researchActionBusy,
    onClick: () => {
      setResearchDeleteConfirm(false);
      setResearchDeleteTarget(null);
    }
  }, "\u041E\u0442\u043C\u0435\u043D\u0430"), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn lp-btn-danger",
    disabled: researchActionBusy,
    onClick: deleteResearch
  }, researchActionBusy ? "Удаляем…" : "Удалить")))));
  return /*#__PURE__*/React.createElement("main", {
    className: "lp-app"
  }, pageHead, tabsBar, view === "catalog" && catalogPanel, view === "ai_research" && researchPanel, view === "queue" && queuePanel, view === "sources" && /*#__PURE__*/React.createElement("div", {
    className: "lp-legacy"
  }, /*#__PURE__*/React.createElement("section", {
    className: "lp-sources-surface",
    id: "lp-panel-sources",
    role: "tabpanel",
    "aria-labelledby": "lp-tab-sources"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-source-grid"
  }, /*#__PURE__*/React.createElement("form", {
    className: "lp-source-card",
    onSubmit: e => {
      e.preventDefault();
      createParserRequest();
    }
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-eyebrow"
  }, "\u0412\u0435\u0431-\u0438\u0441\u0442\u043E\u0447\u043D\u0438\u043A"), /*#__PURE__*/React.createElement("h2", null, "\u041F\u0430\u0440\u0430\u043C\u0435\u0442\u0440\u044B \u0437\u0430\u044F\u0432\u043A\u0438"), /*#__PURE__*/React.createElement("p", {
    className: "lp-muted"
  }, "\u0423\u043A\u0430\u0436\u0438\u0442\u0435 \u0441\u0442\u0440\u0430\u043D\u0438\u0446\u0443 \u0438 \u0442\u0440\u0435\u0431\u043E\u0432\u0430\u043D\u0438\u044F. \u041F\u043E\u0441\u043B\u0435 \u0440\u0430\u0441\u0441\u043C\u043E\u0442\u0440\u0435\u043D\u0438\u044F \u043A\u043E\u043C\u0430\u043D\u0434\u0430 \u0440\u0430\u0437\u0440\u0430\u0431\u043E\u0442\u043A\u0438 \u0441\u043E\u0437\u0434\u0430\u0441\u0442 \u043F\u0430\u0440\u0441\u0435\u0440 \u043E\u0442\u0434\u0435\u043B\u044C\u043D\u043E."), /*#__PURE__*/React.createElement("label", {
    htmlFor: "lp-parser-url"
  }, "URL \u0432\u0435\u0431-\u0438\u0441\u0442\u043E\u0447\u043D\u0438\u043A\u0430"), /*#__PURE__*/React.createElement("input", {
    id: "lp-parser-url",
    type: "url",
    value: newParserUrl,
    onChange: e => {
      setNewParserUrl(e.target.value);
      setParserError("");
    },
    placeholder: "https://bank.example/tariffs"
  }), /*#__PURE__*/React.createElement("label", {
    htmlFor: "lp-parser-description"
  }, "\u0427\u0442\u043E \u0441\u043E\u0431\u0438\u0440\u0430\u0442\u044C"), /*#__PURE__*/React.createElement("textarea", {
    id: "lp-parser-description",
    rows: 4,
    value: newParserDescription,
    onChange: e => {
      setNewParserDescription(e.target.value);
      setParserError("");
    },
    placeholder: "\u0422\u0430\u0440\u0438\u0444\u044B, \u043A\u043E\u043C\u0438\u0441\u0441\u0438\u0438 \u0438 \u0443\u0441\u043B\u043E\u0432\u0438\u044F \u043E\u0431\u0441\u043B\u0443\u0436\u0438\u0432\u0430\u043D\u0438\u044F"
  }), parserError && /*#__PURE__*/React.createElement("div", {
    className: "lp-parser-error",
    role: "alert"
  }, parserError), /*#__PURE__*/React.createElement("button", {
    type: "submit",
    className: "lp-btn lp-btn-primary",
    disabled: parsersBusy || !workspaceId || researchReadOnly || researchLoading || !newParserUrl.trim() || !newParserDescription.trim()
  }, parsersBusy ? "Отправляем…" : "Отправить заявку"), researchReadOnly && /*#__PURE__*/React.createElement("p", {
    className: "lp-muted"
  }, "\u0414\u043B\u044F \u0437\u0430\u044F\u0432\u043A\u0438 \u043E\u0442\u043A\u0440\u043E\u0439\u0442\u0435 \u0438\u043B\u0438 \u0441\u043E\u0437\u0434\u0430\u0439\u0442\u0435 \u0441\u043E\u0431\u0441\u0442\u0432\u0435\u043D\u043D\u043E\u0435 AI-\u0438\u0441\u0441\u043B\u0435\u0434\u043E\u0432\u0430\u043D\u0438\u0435."))), /*#__PURE__*/React.createElement("section", {
    className: "lp-source-list",
    "aria-labelledby": "lp-source-list-title"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-source-list-header"
  }, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("div", {
    className: "lp-eyebrow"
  }, "\u041A\u043E\u043D\u0442\u0440\u043E\u043B\u044C \u0438\u0441\u0442\u043E\u0447\u043D\u0438\u043A\u043E\u0432"), /*#__PURE__*/React.createElement("h2", {
    id: "lp-source-list-title"
  }, "\u041F\u043E\u0434\u043A\u043B\u044E\u0447\u0451\u043D\u043D\u044B\u0435 \u0432\u0435\u0431-\u043F\u0430\u0440\u0441\u0435\u0440\u044B")), /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-btn",
    onClick: loadParsers,
    disabled: parsersLoading
  }, "\u041E\u0431\u043D\u043E\u0432\u0438\u0442\u044C \u0441\u043F\u0438\u0441\u043E\u043A")), parsersLoading ? /*#__PURE__*/React.createElement("div", {
    className: "lp-empty-state"
  }, "\u0417\u0430\u0433\u0440\u0443\u0437\u043A\u0430 \u043F\u0430\u0440\u0441\u0435\u0440\u043E\u0432\u2026") : parsersError ? /*#__PURE__*/React.createElement("div", {
    className: "lp-empty-state"
  }, /*#__PURE__*/React.createElement("p", null, "\u041D\u0435 \u0443\u0434\u0430\u043B\u043E\u0441\u044C \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044C \u043F\u0430\u0440\u0441\u0435\u0440\u044B. \u041F\u0440\u043E\u0432\u0435\u0440\u044C\u0442\u0435 \u0441\u043E\u0435\u0434\u0438\u043D\u0435\u043D\u0438\u0435 \u0438 \u043F\u043E\u0432\u0442\u043E\u0440\u0438\u0442\u0435."), /*#__PURE__*/React.createElement("button", {
    className: "lp-btn",
    onClick: loadParsers
  }, "\u041F\u043E\u0432\u0442\u043E\u0440\u0438\u0442\u044C")) : parsers.length === 0 ? /*#__PURE__*/React.createElement("div", {
    className: "lp-empty-state"
  }, "\u041F\u0430\u0440\u0441\u0435\u0440\u044B \u043D\u0435 \u0441\u043E\u0437\u0434\u0430\u043D\u044B.") : parsers.map(p => {
    const st = p.last_run && p.last_run.status;
    return /*#__PURE__*/React.createElement("article", {
      key: p.parser_id,
      className: "lp-parser-row"
    }, /*#__PURE__*/React.createElement("div", {
      className: "lp-parser-info"
    }, /*#__PURE__*/React.createElement("div", {
      className: "lp-parser-name"
    }, p.name || `Парсер #${p.parser_id}`, p.is_running && /*#__PURE__*/React.createElement("span", {
      className: "lp-badge lp-badge-run"
    }, "\u0432\u044B\u043F\u043E\u043B\u043D\u044F\u0435\u0442\u0441\u044F"), !p.is_running && st === "success" && /*#__PURE__*/React.createElement("span", {
      className: "lp-badge lp-badge-ok"
    }, "\u0443\u0441\u043F\u0435\u0445"), !p.is_running && st === "error" && /*#__PURE__*/React.createElement("span", {
      className: "lp-badge lp-badge-err"
    }, "\u043E\u0448\u0438\u0431\u043A\u0430"), !p.is_running && st === "empty" && /*#__PURE__*/React.createElement("span", {
      className: "lp-badge lp-badge-empty"
    }, "0 \u0440\u0435\u0437\u0443\u043B\u044C\u0442\u0430\u0442\u043E\u0432"), p.needs_attention && /*#__PURE__*/React.createElement("span", {
      className: "lp-badge lp-badge-attn"
    }, "\u0442\u0440\u0435\u0431\u0443\u0435\u0442 \u0432\u043C\u0435\u0448\u0430\u0442\u0435\u043B\u044C\u0441\u0442\u0432\u0430")), p.targets && p.targets.length > 0 && /*#__PURE__*/React.createElement("div", {
      className: "lp-parser-targets"
    }, p.targets.map((target, i) => parserTargetHref(target) ? /*#__PURE__*/React.createElement("a", {
      key: i,
      href: parserTargetHref(target),
      target: "_blank",
      rel: "noopener noreferrer"
    }, target) : /*#__PURE__*/React.createElement("span", {
      key: i,
      className: "lp-parser-target-plain"
    }, target))), /*#__PURE__*/React.createElement("div", {
      className: "lp-parser-meta"
    }, "\u0418\u0441\u0442\u043E\u0447\u043D\u0438\u043A\u043E\u0432 \u0432 \u0431\u0430\u0437\u0435: ", p.records_count ?? 0, p.created_by && ` · автор: ${p.created_by}`), editParserId === p.parser_id && /*#__PURE__*/React.createElement("div", {
      className: "lp-parser-edit"
    }, /*#__PURE__*/React.createElement("label", {
      htmlFor: `lp-parser-name-${p.parser_id}`
    }, "\u041D\u0430\u0437\u0432\u0430\u043D\u0438\u0435", /*#__PURE__*/React.createElement("input", {
      id: `lp-parser-name-${p.parser_id}`,
      type: "text",
      value: editForm.name,
      onChange: e => setEditForm({
        ...editForm,
        name: e.target.value
      })
    })), /*#__PURE__*/React.createElement("label", {
      htmlFor: `lp-parser-cron-${p.parser_id}`
    }, "\u0420\u0430\u0441\u043F\u0438\u0441\u0430\u043D\u0438\u0435 (cron)", /*#__PURE__*/React.createElement("input", {
      id: `lp-parser-cron-${p.parser_id}`,
      type: "text",
      placeholder: "0 5 * * *",
      value: editForm.cron_expr,
      disabled: !editForm.auto_enabled,
      onChange: e => setEditForm({
        ...editForm,
        cron_expr: e.target.value
      })
    })), /*#__PURE__*/React.createElement("label", {
      className: "lp-parser-edit-toggle",
      htmlFor: `lp-parser-auto-${p.parser_id}`
    }, /*#__PURE__*/React.createElement("input", {
      id: `lp-parser-auto-${p.parser_id}`,
      type: "checkbox",
      checked: editForm.auto_enabled,
      onChange: e => setEditForm({
        ...editForm,
        auto_enabled: e.target.checked
      })
    }), "\u0410\u0432\u0442\u043E\u0437\u0430\u043F\u0443\u0441\u043A \u0432\u043A\u043B\u044E\u0447\u0451\u043D"), editError && /*#__PURE__*/React.createElement("div", {
      className: "lp-parser-error"
    }, editError), /*#__PURE__*/React.createElement("div", {
      className: "lp-parser-edit-actions"
    }, /*#__PURE__*/React.createElement("button", {
      className: "lp-btn lp-btn-sm lp-btn-primary",
      onClick: saveEdit,
      disabled: parsersBusy
    }, "\u0421\u043E\u0445\u0440\u0430\u043D\u0438\u0442\u044C"), /*#__PURE__*/React.createElement("button", {
      className: "lp-btn lp-btn-sm",
      onClick: () => setEditParserId(null)
    }, "\u041E\u0442\u043C\u0435\u043D\u0430"), /*#__PURE__*/React.createElement("button", {
      className: "lp-btn lp-btn-sm",
      onClick: () => healParser(p.parser_id),
      disabled: parsersBusy
    }, "\u0410\u043D\u0430\u043B\u0438\u0437 \u0438 \u0432\u043E\u0441\u0441\u0442\u0430\u043D\u043E\u0432\u043B\u0435\u043D\u0438\u0435")))));
  })))), tabPlaceholders, popLayer, accessSheet, verdictDialog, researchDeleteDialog, deleteConfirm && /*#__PURE__*/React.createElement("div", {
    className: "lp-parsers-modal"
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-modal-backdrop",
    "aria-label": "\u0417\u0430\u043A\u0440\u044B\u0442\u044C \u0434\u0438\u0430\u043B\u043E\u0433",
    tabIndex: -1,
    onClick: () => setDeleteConfirm(null)
  }), /*#__PURE__*/React.createElement("div", {
    className: "lp-parsers-dialog lp-confirm-dialog",
    ref: confirmDialogRef,
    role: "dialog",
    "aria-modal": "true",
    "aria-labelledby": "lp-confirm-title"
  }, /*#__PURE__*/React.createElement("div", {
    className: "lp-parsers-header"
  }, /*#__PURE__*/React.createElement("h2", {
    id: "lp-confirm-title"
  }, "\u0423\u0434\u0430\u043B\u0435\u043D\u0438\u0435 \u043F\u0430\u0440\u0441\u0435\u0440\u0430"), /*#__PURE__*/React.createElement("button", {
    className: "lp-dialog-x",
    "aria-label": "\u0417\u0430\u043A\u0440\u044B\u0442\u044C",
    onClick: () => setDeleteConfirm(null)
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "x"
  }))), /*#__PURE__*/React.createElement("div", {
    className: "lp-confirm-body"
  }, /*#__PURE__*/React.createElement("p", null, "\u041F\u0430\u0440\u0441\u0435\u0440 \xAB", deleteConfirm.name || `Парсер #${deleteConfirm.parser_id}`, "\xBB \u0431\u0443\u0434\u0435\u0442 \u0443\u0434\u0430\u043B\u0451\u043D \u0432\u043C\u0435\u0441\u0442\u0435 \u0441 \u043A\u043E\u0434\u043E\u043C \u0438 \u0437\u0430\u043F\u0438\u0441\u044C\u044E. \u0414\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u043D\u0435\u043E\u0431\u0440\u0430\u0442\u0438\u043C\u043E."), /*#__PURE__*/React.createElement("div", {
    className: "lp-confirm-actions"
  }, /*#__PURE__*/React.createElement("button", {
    className: "lp-btn lp-btn-danger",
    onClick: confirmDeleteParser,
    disabled: parsersBusy
  }, "\u0423\u0434\u0430\u043B\u0438\u0442\u044C"), /*#__PURE__*/React.createElement("button", {
    className: "lp-btn",
    ref: confirmCancelRef,
    onClick: () => setDeleteConfirm(null)
  }, "\u041E\u0442\u043C\u0435\u043D\u0430"))))), toast && /*#__PURE__*/React.createElement("div", {
    key: toast.id,
    className: "lp-toast lp-toast-" + toast.kind,
    role: toast.kind === "error" ? "alert" : "status"
  }, toast.kind === "success" && /*#__PURE__*/React.createElement(Icon, {
    name: "check"
  }), toast.kind === "error" && /*#__PURE__*/React.createElement(Icon, {
    name: "alert"
  }), /*#__PURE__*/React.createElement("span", null, toast.text), toast.undo && /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-toast-action",
    onClick: () => {
      const undo = toast.undo;
      setToast(null);
      undo();
    }
  }, "\u041E\u0442\u043C\u0435\u043D\u0438\u0442\u044C"), toast.undo && /*#__PURE__*/React.createElement("i", {
    className: "lp-toast-ttl",
    "aria-hidden": "true",
    style: {
      animationDuration: `${toast.ttl}ms`
    }
  }), !toast.undo && toast.kind === "success" && toast.text.startsWith("Excel сформирован") && lastCsvDownload && /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "lp-toast-action",
    onClick: () => triggerCsvDownload(lastCsvDownload)
  }, "\u0421\u043A\u0430\u0447\u0430\u0442\u044C \u043F\u043E\u0432\u0442\u043E\u0440\u043D\u043E")));
}
const root = ReactDOM.createRoot(document.getElementById("loophole-root"));
root.render(/*#__PURE__*/React.createElement(LoopholeApp, null));
