// Сборка интерфейса: JSX → JS заранее, а не в браузере у каждого посетителя.
// Раньше страница тянула 3 МБ Babel и компилировала 540 КБ исходника на месте —
// главный поток стоял три секунды, и всё это время интерфейс не отвечал.
// Babel берём тот же, что лежал в vendor: никаких новых зависимостей.
const fs = require("fs"), path = require("path");
const root = path.join(__dirname, "..", "src", "bank_audit", "web", "static");
const Babel = require(path.join(root, "vendor", "babel.min.js"));
const src = fs.readFileSync(path.join(root, "app.jsx"), "utf8");
const t0 = Date.now();
const { code } = Babel.transform(src, { presets: ["react"], filename: "app.jsx", compact: false });
const out = path.join(root, "app.js");
fs.writeFileSync(out, "// Собрано из app.jsx: scripts/build_frontend.js. Правьте .jsx, не этот файл.\n" + code);
console.log(`собрано за ${Date.now() - t0} мс: ${(code.length / 1024).toFixed(0)} КБ → ${path.relative(process.cwd(), out)}`);
