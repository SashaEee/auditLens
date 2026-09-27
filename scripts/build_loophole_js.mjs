// Предсборка вкладки «Уязвимости»: loophole.jsx → loophole.js тем же Babel,
// что лежит в vendor (preset react). Страница больше не собирает 200 КБ JSX
// в браузере: это стоило 1,3 с на обычном ПК и 5,5 с на слабом.
//
// Запуск после любой правки loophole.jsx:  node scripts/build_loophole_js.mjs
// В шапке собранного файла — sha256 исходника; тест test_loophole_js_is_built
// падает, если JSX поменяли, а сборку не обновили.
import {createHash} from "node:crypto";
import {readFileSync, writeFileSync} from "node:fs";
import {dirname, join} from "node:path";
import {fileURLToPath} from "node:url";
import vm from "node:vm";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const staticDir = join(root, "src", "bank_audit", "loophole", "static");
const source = readFileSync(join(staticDir, "loophole.jsx"), "utf8");
const sandbox = {console};
sandbox.window = sandbox;
sandbox.self = sandbox;
vm.createContext(sandbox);
vm.runInContext(readFileSync(join(root, "src", "bank_audit", "web", "static", "vendor", "babel.min.js"), "utf8"), sandbox);
const {code} = sandbox.Babel.transform(source, {presets: ["react"], filename: "loophole.jsx"});
const sha = createHash("sha256").update(source, "utf8").digest("hex");
const header = `/* Собрано из loophole.jsx (sha256 ${sha}). Не править вручную: node scripts/build_loophole_js.mjs */\n`;
writeFileSync(join(staticDir, "loophole.js"), header + code + "\n");
console.log(`loophole.js: ${(code.length / 1024).toFixed(0)} КБ, sha256 ${sha.slice(0, 12)}`);
