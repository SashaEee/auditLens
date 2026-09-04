// Сборка интерфейса: JSX → JS заранее, а не в браузере у каждого посетителя.
// Раньше страница тянула 3 МБ Babel и компилировала 540 КБ исходника на месте —
// главный поток стоял три секунды, и всё это время интерфейс не отвечал.
// Babel берём тот же, что лежал в vendor: никаких новых зависимостей.
const fs = require("fs"), path = require("path");
const root = path.join(__dirname, "..", "src", "bank_audit", "web", "static");
const Babel = require(path.join(root, "vendor", "babel.min.js"));
const src = fs.readFileSync(path.join(root, "app.jsx"), "utf8");
const t0 = Date.now();
// Babel проверяет только синтаксис: файл, где вызывается удалённая функция,
// компилируется молча и падает уже в браузере — так однажды и слёг весь
// интерфейс. Поэтому перед сборкой сверяем вызовы хуков с определениями.
const called = new Set([...src.matchAll(/^\s{2}(use[A-Z]\w+)\(\);/gm)].map(m => m[1]));
const defined = new Set([...src.matchAll(/^function (use[A-Z]\w+)\(/gm)].map(m => m[1]));
const builtin = new Set(["useState", "useEffect", "useRef", "useMemo", "useCallback", "useContext"]);
const missing = [...called].filter(n => !defined.has(n) && !builtin.has(n));
if (missing.length) {
  console.error("сборка остановлена: вызывается, но нигде не определено — " + missing.join(", "));
  process.exit(1);
}

const { code } = Babel.transform(src, { presets: ["react"], filename: "app.jsx", compact: false });
const out = path.join(root, "app.js");
fs.writeFileSync(out, "// Собрано из app.jsx: scripts/build_frontend.js. Правьте .jsx, не этот файл.\n" + code);
console.log(`собрано за ${Date.now() - t0} мс: ${(code.length / 1024).toFixed(0)} КБ → ${path.relative(process.cwd(), out)}`);
