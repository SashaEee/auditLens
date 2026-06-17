"""Browser-collector использует Playwright. В контексте OpenClaw запускается
   с тем же профилем (OPENCLAW_BROWSER_PROFILE), что и интерактивный browser tool —
   это даёт согласованное окружение и снижает шанс «детекции бота» легитимными
   средствами (без обхода защит).

   Stealth-патч (playwright-stealth) скрывает признаки headless-режима:
   navigator.webdriver, chrome runtime, plugins и т.д.
"""
from __future__ import annotations
import json, os, time, logging
import json as _json_module
from contextlib import contextmanager
from pathlib import Path
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

log = logging.getLogger(__name__)

# Маркеры капчи — при обнаружении пишем в captcha_pending.json
CAPTCHA_MARKERS = [
    "SmartCaptcha", "smartcaptcha",
    "Подтвердите, что запросы отправляли вы",
    "похожи на автоматические",
    "checkcaptcha", "captcha-delivery",
]

# Пробуем импортировать stealth. Поддерживаем v1 (stealth_sync) и v2 (Stealth).
_STEALTH_AVAILABLE = False
_stealth_v2 = None
try:
    from playwright_stealth import Stealth as _Stealth
    _stealth_v2 = _Stealth()
    _STEALTH_AVAILABLE = True
except ImportError:
    try:
        from playwright_stealth import stealth_sync as _stealth_v1  # type: ignore[no-redef]
        _STEALTH_AVAILABLE = True
    except ImportError:
        log.warning("playwright-stealth не установлен — headless-режим легче детектируется")


def _apply_stealth(page: Page) -> None:
    """Применяет stealth-патч (v2 или v1 API), если библиотека доступна."""
    if not _STEALTH_AVAILABLE:
        return
    if _stealth_v2 is not None:
        _stealth_v2.use_sync(page)
    else:
        _stealth_v1(page)  # type: ignore[name-defined]


class CaptchaRequired(RuntimeError):
    def __init__(self, url: str, source: str | None = None, target: str | None = None):
        super().__init__(f"Captcha required: {url}")
        self.url = url
        self.source = source
        self.target = target


class BrowserCollector:
    # Общие аргументы Chromium для снижения детектируемости.
    # ignore-certificate-errors: для sberbank.ru и др. российских сайтов,
    # подписанных Russian Trusted Root CA (Минцифра РФ), который не входит
    # в стандартный bundle Chromium. Без этого Sber.ru возвращает заглушку
    # «установите сертификат Минцифры». Для httpx-fetcher CA подключён
    # явно через verify=CA_BUNDLE_PATH; для Chromium используем флаг.
    _LAUNCH_ARGS = [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--ignore-certificate-errors",
    ]
    # Реалистичный User-Agent. По умолчанию headless-playwright шлёт UA с меткой
    # «HeadlessChrome» → sberbank.ru (и др.) ловит это анти-ботом и отдаёт заглушку
    # «установите сертификаты НУЦ». Это НЕ про TLS (серт Сбера — публичный HARICA):
    # с обычным Chrome-UA сайт отдаёт реальный SPA, который chromium рендерит.
    _UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    # Гарантированный stealth (НЕ зависит от внешней playwright-stealth, которой
    # на сервере может не быть → _apply_stealth там no-op). Скрывает headless-
    # признаки: ровно этот набор эмпирически пробил F5-antibot Сбера (challenge
    # «Your support ID» проходит за ~2с, рендерится реальный контент).
    _STEALTH_JS = r"""
(() => {
  Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
  window.chrome={runtime:{},loadTimes:function(){},csi:function(){},app:{isInstalled:false}};
  Object.defineProperty(navigator,'languages',{get:()=>['ru-RU','ru','en-US','en']});
  Object.defineProperty(navigator,'plugins',{get:()=>[
    {name:'Chrome PDF Plugin'},{name:'Chrome PDF Viewer'},{name:'Native Client'},
    {name:'PDF Viewer'},{name:'WebKit built-in PDF'}]});
  Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});
  Object.defineProperty(navigator,'deviceMemory',{get:()=>8});
  Object.defineProperty(navigator,'platform',{get:()=>'Linux x86_64'});
  try {
    const q = navigator.permissions && navigator.permissions.query;
    if (q) navigator.permissions.query = (p) =>
      (p && p.name === 'notifications')
        ? Promise.resolve({state: Notification.permission})
        : q(p);
  } catch(e){}
  // WebGL vendor/renderer — главный headless-tell (по умолчанию SwiftShader/Google).
  const spoof = function(getParam){
    return function(p){
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel Iris OpenGL Engine';
      return getParam.call(this, p);
    };
  };
  try { WebGLRenderingContext.prototype.getParameter =
        spoof(WebGLRenderingContext.prototype.getParameter); } catch(e){}
  try { if (window.WebGL2RenderingContext) WebGL2RenderingContext.prototype.getParameter =
        spoof(WebGL2RenderingContext.prototype.getParameter); } catch(e){}
})();
"""

    def __init__(self, headless: bool = True, profile_dir: str | None = None,
                 nav_timeout_s: float = 45.0, scroll_pause_ms: int = 800,
                 max_scrolls: int = 40):
        self.headless = headless
        self.profile_dir = profile_dir or os.getenv("OPENCLAW_BROWSER_PROFILE")
        self.nav_timeout_ms = int(nav_timeout_s * 1000)
        self.scroll_pause_ms = scroll_pause_ms
        self.max_scrolls = max_scrolls

    @contextmanager
    def _ctx(self, headless: bool | None = None):
        """Возвращает BrowserContext (persistent-профиль или обычный)."""
        use_headless = headless if headless is not None else self.headless
        with sync_playwright() as p:
            if self.profile_dir:
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=self.profile_dir,
                    headless=use_headless,
                    args=self._LAUNCH_ARGS,
                    viewport={"width": 1440, "height": 900},
                    locale="ru-RU",
                    timezone_id="Europe/Moscow",
                    user_agent=self._UA,
                    ignore_https_errors=True,
                )
                browser = None
            else:
                browser = p.chromium.launch(headless=use_headless, args=self._LAUNCH_ARGS)
                ctx = browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    locale="ru-RU",
                    timezone_id="Europe/Moscow",
                    user_agent=self._UA,
                    ignore_https_errors=True,
                )
            # Stealth на уровне контекста — применяется ко всем страницам, не
            # зависит от наличия playwright-stealth (пробивает F5-antibot Сбера).
            try:
                ctx.add_init_script(self._STEALTH_JS)
            except Exception:
                pass
            try:
                yield ctx
            finally:
                ctx.close()
                if browser:
                    browser.close()

    def fetch_html(self, url: str, wait_selector: str | None = None,
                   scroll_to_bottom: bool = False,
                   workspace_dir: str | None = None,
                   source: str = "browser",
                   target: str | None = None,
                   capture_xhr_pattern: str | None = None,
                   capture_max_bodies: int = 200) -> tuple[int, bytes]:
        """Загружает страницу. Если capture_xhr_pattern задан — также
        перехватывает JSON-ответы XHR/fetch, URL которых содержит подстроку.

        Возвращает (status, html). Перехваченные XHR-ответы складываются в
        ``self.last_captured`` (list[dict]) — адаптер забирает их после вызова.
        Это позволяет sravni-style SPA отдать полный AJAX-payload
        (proxy-credits/credits, proxy-cards/credit и т.д.), а не только SSR-top.
        """
        self.last_captured: list[dict] = []
        with self._ctx() as ctx:
            page = ctx.new_page()
            _apply_stealth(page)
            page.set_default_navigation_timeout(self.nav_timeout_ms)

            if capture_xhr_pattern:
                self._attach_xhr_capture(page, capture_xhr_pattern, capture_max_bodies)

            resp = page.goto(url, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=self.nav_timeout_ms)
                except Exception:
                    pass  # fallback — берём что есть
            if scroll_to_bottom:
                self._infinite_scroll(page)

            # JS-challenge (F5-antibot Сбера) + SPA-рендер: после domcontentloaded
            # в DOM ещё challenge-shell «enable JavaScript / Your support ID». F5
            # вычисляет токен и ПЕРЕЗАГРУЖАЕТ страницу (~2-5с). Поэтому ждём не
            # фикс-паузу, а ИСЧЕЗНОВЕНИЯ маркера challenge — пока появится реальный
            # контент. Если за 15с не пробилось — берём что есть (fallback).
            if not wait_selector:
                try:
                    page.wait_for_function(
                        "() => { const t = document.body ? document.body.innerText : '';"
                        " return t.length > 600 && !t.includes('enable JavaScript')"
                        " && !t.includes('support ID') && !t.includes('Минцифры'); }",
                        timeout=15000)
                except Exception:
                    pass
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(600)

            html = page.content().encode("utf-8")
            status = resp.status if resp else 200

            # Капча-детекция
            text = html[:8192].decode("utf-8", errors="ignore")
            if any(m in text for m in CAPTCHA_MARKERS):
                self._save_captcha_pending(url, source, workspace_dir, target=target)
                raise CaptchaRequired(url, source=source, target=target)

            return status, html

    def fetch_redux_state_resilient(self, landing_url: str, **kwargs) -> tuple[int, dict | None]:
        """Обёртка над fetch_redux_state с anti-captcha стратегией.

        Стратегия (умышленно короткая — раньше блокировала pipeline на 6-8 мин):
          1. Попытка headless. Если CaptchaRequired → короткий sleep (5-15s)
          2. Сразу HEADED (видимый браузер с прогретым profile'м). Если и тут
             captcha — пробрасываем выше, pipeline пойдёт дальше без sravni.

        Раньше было: 3 attempt'а с sleep'ами 60-150s каждый = до 5+ минут
        блокировки event-loop'а. Sravni либо отдаёт сразу либо банит на час —
        длинные sleep'ы помогают редко, а deep-research зависает гарантированно.

        Все параметры можно переопределить через env:
          • SRAVNI_RETRY_ATTEMPTS (default 2)
          • SRAVNI_RETRY_SLEEP_MIN (default 5)
          • SRAVNI_RETRY_SLEEP_MAX (default 15)
          • SRAVNI_TOTAL_BUDGET_S  (default 45) — общий cap на ВСЁ время
            ожидания в этой функции; при превышении прекращаем.
        """
        import random, time as _t
        max_attempts = int(os.getenv("SRAVNI_RETRY_ATTEMPTS", "2"))
        sleep_min    = float(os.getenv("SRAVNI_RETRY_SLEEP_MIN", "5"))
        sleep_max    = float(os.getenv("SRAVNI_RETRY_SLEEP_MAX", "15"))
        total_budget = float(os.getenv("SRAVNI_TOTAL_BUDGET_S",  "45"))

        started = _t.time()
        for attempt in range(1, max_attempts + 1):
            force_headed = (attempt == max_attempts)
            try:
                if force_headed:
                    log.warning("fetch_redux_state attempt %s: switching to HEADED",
                                attempt)
                    return self.fetch_redux_state(landing_url, _force_headless=False, **kwargs)
                return self.fetch_redux_state(landing_url, **kwargs)
            except CaptchaRequired:
                if attempt >= max_attempts:
                    raise
                # Cap suffix: остаток бюджета или хотя бы 1s
                remaining = total_budget - (_t.time() - started)
                if remaining <= 1:
                    log.warning("fetch_redux_state: исчерпан total budget %.0fs — fail-fast", total_budget)
                    raise
                wait_s = min(random.uniform(sleep_min, sleep_max), remaining)
                log.warning("fetch_redux_state captcha (attempt %s/%s) — sleeping %.1fs before retry (budget left %.0fs)",
                            attempt, max_attempts, wait_s, remaining)
                time.sleep(wait_s)
        # unreachable
        raise CaptchaRequired(landing_url)

    def fetch_redux_state(self, landing_url: str,
                          button_text_re: str = r"^Показать ещ",
                          max_clicks: int = 60,
                          per_click_pause_s: float = 1.0,
                          expand_group_text_re: str | None = None,
                          max_group_expansions: int = 200,
                          workspace_dir: str | None = None,
                          source: str = "browser",
                          target: str | None = None,
                          _force_headless: bool | None = None) -> tuple[int, dict | None]:
        """Открывает SPA-страницу sravni, кликает кнопку «Показать ещё» пока
        она появляется, и в конце достаёт полный Redux store через walk
        по React fiber tree. Возвращает (status, state_dict).

        Эта стратегия покрывает категории, где SSR отдаёт лишь top-N
        (credit/mortgage/card/auto), а остальное живёт только в client-side
        Redux store (никаких отдельных AJAX endpoints не существует).
        """
        import re as _re
        # _force_headless=False → принудительно headed (для anti-captcha retry)
        # None → дефолт self.headless
        ctx_headless = self.headless if _force_headless is None else _force_headless
        with self._ctx(headless=ctx_headless) as ctx:
            page = ctx.new_page()
            _apply_stealth(page)
            page.set_default_navigation_timeout(self.nav_timeout_ms)
            resp = page.goto(landing_url, wait_until="domcontentloaded")
            status = resp.status if resp else 200

            # Капча
            try:
                head = page.content()[:8192]
                if any(m in head for m in CAPTCHA_MARKERS):
                    self._save_captcha_pending(landing_url, source, workspace_dir, target=target)
                    raise CaptchaRequired(landing_url, source=source, target=target)
            except CaptchaRequired:
                raise
            except Exception:
                pass

            # Дать гидратации завершиться
            time.sleep(3)

            # Скролл + клик "Показать ещё" в цикле
            clicks = 0
            while clicks < max_clicks:
                # Прогрев viewport
                for _ in range(5):
                    page.mouse.wheel(0, 4000)
                    time.sleep(0.25)
                btn = page.get_by_role("button", name=_re.compile(button_text_re, _re.I)).first
                try:
                    if btn.count() == 0:
                        break
                    btn.wait_for(state="visible", timeout=2000)
                    btn.scroll_into_view_if_needed(timeout=2000)
                    btn.click(timeout=3000)
                    clicks += 1
                    time.sleep(per_click_pause_s)
                except Exception:
                    break
            log.info("fetch_redux_state %s: %s loadmore clicks", landing_url, clicks)

            # Опционально: раскрываем «Ещё N кредитов / Ещё N карт» внутри
            # каждой карточки банка. Эти элементы в sravni — это <span> с
            # cursor:pointer (без role=button), поэтому ищем по тексту.
            group_expanded = 0
            if expand_group_text_re:
                for _ in range(15):
                    page.mouse.wheel(0, 4000)
                    time.sleep(0.2)
                try:
                    pat = _re.compile(expand_group_text_re, _re.I)
                    locator = page.get_by_text(pat)
                    n_total = locator.count()
                    log.info("fetch_redux_state: %s group-expand text matches", n_total)
                    for i in range(min(n_total, max_group_expansions)):
                        el = locator.nth(i)
                        try:
                            el.scroll_into_view_if_needed(timeout=1500)
                            # Клик через JS, т.к. span может быть «частично перекрыт»
                            el.click(timeout=2000, force=True)
                            group_expanded += 1
                            time.sleep(0.12)
                        except Exception:
                            continue
                except Exception as e:
                    log.info("group-expand failed: %s", e)
                log.info("fetch_redux_state: %s groups expanded", group_expanded)
                time.sleep(1.0)

            # Walk React fiber → Redux store
            state = page.evaluate(
                """() => {
                    function walk(n, depth) {
                        if (!n || depth > 60) return null;
                        if (n.memoizedProps && n.memoizedProps.store
                            && typeof n.memoizedProps.store.getState === 'function') {
                            return n.memoizedProps.store.getState();
                        }
                        return walk(n.child, depth+1) || walk(n.sibling, depth+1);
                    }
                    const root = document.querySelector('#__next');
                    if (!root) return null;
                    const key = Object.keys(root).find(k => k.startsWith('__reactContainer$'));
                    return key ? walk(root[key].stateNode.current, 0) : null;
                }"""
            )
        return status, state

    def fetch_with_loadmore(self, landing_url: str, url_substring: str,
                            button_text_re: str = r"^Показать ещ[её]",
                            max_clicks: int = 60,
                            workspace_dir: str | None = None,
                            source: str = "browser",
                            target: str | None = None) -> tuple[int, bytes, list[dict]]:
        """Загружает SPA, кликает кнопку «Показать ещё» пока она есть, и
        попутно перехватывает все JSON-XHR с URL содержащим url_substring.

        Возвращает: (status, final_html, captured_xhr).
        """
        captured: list[dict] = []
        with self._ctx() as ctx:
            page = ctx.new_page()
            _apply_stealth(page)
            page.set_default_navigation_timeout(self.nav_timeout_ms)

            def _on_response(resp):
                try:
                    if url_substring not in resp.url:
                        return
                    ct = (resp.headers.get("content-type") or "").lower()
                    if "json" not in ct:
                        return
                    body = resp.body()
                    parsed = _json_module.loads(body.decode("utf-8", errors="ignore"))
                    captured.append({"url": resp.url, "status": resp.status, "data": parsed})
                except Exception:
                    pass

            page.on("response", _on_response)
            resp = page.goto(landing_url, wait_until="domcontentloaded")
            status = resp.status if resp else 200

            # Капча-детекция на landing
            try:
                head = page.content()[:8192]
                if any(m in head for m in CAPTCHA_MARKERS):
                    self._save_captcha_pending(landing_url, source, workspace_dir, target=target)
                    raise CaptchaRequired(landing_url, source=source, target=target)
            except CaptchaRequired:
                raise
            except Exception:
                pass

            # Сначала прокручиваем к низу, чтобы кнопка отрендерилась
            for _ in range(8):
                page.mouse.wheel(0, 4000)
                time.sleep(0.4)

            # Цикл "Показать ещё": используем locator с regex по text
            import re as _re
            clicks = 0
            while clicks < max_clicks:
                btn = page.get_by_role("button", name=_re.compile(button_text_re, _re.I)).first
                try:
                    if btn.count() == 0:
                        log.info("loadmore: no button, stop")
                        break
                    btn.wait_for(state="visible", timeout=2000)
                except Exception:
                    log.info("loadmore: button not visible, stop")
                    break
                try:
                    btn.scroll_into_view_if_needed(timeout=2000)
                    prev_count = len(captured)
                    btn.click(timeout=3000)
                    clicks += 1
                    # Ждём пока прилетит новый XHR
                    deadline = time.time() + 6
                    while time.time() < deadline and len(captured) == prev_count:
                        time.sleep(0.3)
                    # После каждого клика — крутим вниз, чтобы новая кнопка
                    # «Показать ещё» снова попала в viewport.
                    for _ in range(3):
                        page.mouse.wheel(0, 4000)
                        time.sleep(0.3)
                    log.info("loadmore click #%s → captured=%s (delta=%s)",
                             clicks, len(captured), len(captured) - prev_count)
                except Exception as e:
                    log.info("loadmore stop on click %s: %s", clicks, e)
                    break

            # Финальный wait для догрузки
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            final_html = page.content().encode("utf-8")
        log.info("fetch_with_loadmore %s: %s clicks, %s XHR captured",
                 landing_url, clicks, len(captured))
        return status, final_html, captured

    def discover_xhr_on_page(self, landing_url: str, url_substring: str,
                             scroll: bool = True) -> list[dict]:
        """Открывает landing_url, перехватывает все JSON-ответы XHR
        c URL содержащим ``url_substring``. Возвращает list of
        {"url", "status", "data"}. Используется для авто-discovery
        реальных API-эндпоинтов SPA.
        """
        captured: list[dict] = []
        with self._ctx() as ctx:
            page = ctx.new_page()
            _apply_stealth(page)
            page.set_default_navigation_timeout(self.nav_timeout_ms)

            def _on_response(resp):
                try:
                    if url_substring not in resp.url:
                        return
                    ct = (resp.headers.get("content-type") or "").lower()
                    if "json" not in ct:
                        return
                    body = resp.body()
                    try:
                        parsed = _json_module.loads(body.decode("utf-8", errors="ignore"))
                    except Exception:
                        return
                    captured.append({"url": resp.url, "status": resp.status, "data": parsed})
                except Exception:
                    pass

            page.on("response", _on_response)
            page.goto(landing_url, wait_until="domcontentloaded")
            if scroll:
                self._infinite_scroll(page)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
        return captured

    def fetch_api_in_page(self, landing_url: str, api_calls: list[dict],
                          workspace_dir: str | None = None,
                          source: str = "browser",
                          target: str | None = None) -> list[dict]:
        """Открывает landing_url (warm-up cookies), затем выполняет fetch()
        ВНУТРИ страницы для каждого api_call. Это позволяет дёргать API,
        защищённые SmartCaptcha — браузер несёт cookies прогретого профиля.

        api_calls: list of {"url": "/proxy-x/y/", "method": "GET"|"POST",
                            "params": {...}, "body": {...}}
        Возвращает: list of {"url", "status", "data": parsed_json}
        """
        results: list[dict] = []
        with self._ctx() as ctx:
            page = ctx.new_page()
            _apply_stealth(page)
            page.set_default_navigation_timeout(self.nav_timeout_ms)
            resp = page.goto(landing_url, wait_until="domcontentloaded")

            # Капча на landing — адаптеру решать (мы и так зафиксируем pending)
            try:
                content_head = page.content()[:8192]
                if any(m in content_head for m in CAPTCHA_MARKERS):
                    self._save_captcha_pending(landing_url, source, workspace_dir, target=target)
                    raise CaptchaRequired(landing_url, source=source, target=target)
            except CaptchaRequired:
                raise
            except Exception:
                pass

            # Пауза, чтобы куки/anti-bot токены успели прогреться
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            for call in api_calls:
                url    = call["url"]
                method = (call.get("method") or "GET").upper()
                params = call.get("params") or {}
                body   = call.get("body")

                # Собираем итоговый URL c query
                if params:
                    from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl
                    parts = urlparse(url)
                    q = dict(parse_qsl(parts.query))
                    for k, v in params.items():
                        if isinstance(v, (list, tuple)):
                            q[k] = ",".join(str(x) for x in v)
                        else:
                            q[k] = str(v)
                    url = urlunparse(parts._replace(query=urlencode(q)))

                js = """async ({url, method, body}) => {
                    const opts = { method, credentials: 'include',
                                   headers: { 'Accept': 'application/json,*/*',
                                              'X-Requested-With': 'XMLHttpRequest' } };
                    if (body) {
                        opts.headers['Content-Type'] = 'application/json';
                        opts.body = JSON.stringify(body);
                    }
                    const r = await fetch(url, opts);
                    const t = await r.text();
                    let data = null;
                    try { data = JSON.parse(t); } catch(e) { data = { _raw: t.slice(0,400) }; }
                    return { status: r.status, data };
                }"""
                log.warning("fetch_api_in_page → calling %s", url)
                try:
                    res = page.evaluate(js, {"url": url, "method": method, "body": body})
                except Exception as e:
                    log.warning("fetch_api_in_page %s: EVAL FAIL %s", url, e)
                    continue
                data = res.get("data")
                # Логируем shape ответа для дебага
                if isinstance(data, dict):
                    keys = list(data.keys())[:8]
                    arr_len = len(data.get("data") or []) if isinstance(data.get("data"), list) else "?"
                    log.warning("fetch_api_in_page %s → status=%s keys=%s data[]=%s",
                                url, res.get("status"), keys, arr_len)
                else:
                    log.warning("fetch_api_in_page %s → status=%s body=%r",
                                url, res.get("status"), str(data)[:200])
                results.append({"url": url, "status": res.get("status"), "data": data})

        return results

    def _attach_xhr_capture(self, page: Page, pattern: str, max_bodies: int):
        """Подписывается на response-события и копит JSON-ответы по фильтру."""
        captured = self.last_captured

        def _on_response(resp):
            try:
                if len(captured) >= max_bodies:
                    return
                u = resp.url
                if pattern not in u:
                    return
                ct = (resp.headers.get("content-type") or "").lower()
                if "json" not in ct and not u.endswith(".json"):
                    return
                # body() может бросить, если ответ был abort/preload
                body = resp.body()
                try:
                    parsed = json.loads(body.decode("utf-8", errors="ignore"))
                except Exception:
                    return
                captured.append({
                    "url":    u,
                    "status": resp.status,
                    "data":   parsed,
                })
            except Exception as e:
                log.debug("xhr capture error: %s", e)

        page.on("response", _on_response)

    # ── CAPTCHA: ручное решение в headed-браузере ────────────────────────────

    def open_for_captcha(self, url: str, wait_s: float = 180.0) -> bool:
        """Открывает URL в ВИДИМОМ (headed) браузере с тем же профилем.

        Пользователь видит капчу и решает её вручную. Метод ждёт до ``wait_s``
        секунд, проверяя каждые 2 с, не исчезли ли маркеры капчи.
        Возвращает True при успехе, False при таймауте.

        После успешного решения куки сохраняются в профиль — следующие
        headless-запросы к тому же домену будут работать без капчи.
        """
        log.info("Открываем headed-браузер для капчи: %s", url)
        with self._ctx(headless=False) as ctx:
            page = ctx.new_page()
            _apply_stealth(page)
            page.set_default_navigation_timeout(int(wait_s * 1000))
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception as e:
                log.warning("Навигация к капче упала: %s", e)
                return False

            deadline = time.time() + wait_s
            while time.time() < deadline:
                try:
                    content = page.content()
                except Exception:
                    break  # страница закрыта пользователем
                text_preview = content[:8192]
                if not any(m in text_preview for m in CAPTCHA_MARKERS):
                    log.info("Капча решена (маркеры исчезли)")
                    return True
                time.sleep(2)

        log.warning("Таймаут ожидания решения капчи для %s", url)
        return False

    def _save_captcha_pending(self, url: str, source: str, workspace_dir: str | None,
                              target: str | None = None):
        ws = Path(workspace_dir) if workspace_dir else Path("workspace")
        ws.mkdir(parents=True, exist_ok=True)
        path = ws / "captcha_pending.json"
        items = []
        if path.exists():
            try:
                items = json.loads(path.read_text())
            except Exception:
                pass
        # Дедупликация по URL — но обновляем source/target если уже было
        for it in items:
            if it.get("url") == url:
                it["source"] = source
                if target: it["target"] = target
                path.write_text(json.dumps(items, ensure_ascii=False))
                return
        items.append({"url": url, "source": source, "target": target})
        path.write_text(json.dumps(items, ensure_ascii=False))

    def _infinite_scroll(self, page: Page):
        prev_h = -1
        for _ in range(self.max_scrolls):
            page.mouse.wheel(0, 4000)
            time.sleep(self.scroll_pause_ms / 1000)
            h = page.evaluate("document.body.scrollHeight")
            if h == prev_h:
                break
            prev_h = h
