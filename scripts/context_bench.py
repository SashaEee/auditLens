"""Стенд гигиены контекста: что парсер отдаёт модели (этап 1).

Гоняет сохранённые фикстуры через боевой парсер и считает метрики, по которым
дальше принимаются решения. Ключевая идея разметки: числа-ловушки размечаются
НЕ вручную, а структурно — по исходному DOM. Число внутри form/select/фильтра
это UI-элемент («от 100 000 ₽» в панели фильтров), число в тексте или таблице —
кандидат в условие продукта. Разметка объективна и воспроизводима.

Метрики на страницу:
  chars           — сколько символов уходит модели
  short_lines_pct — доля строк короче 40 символов (меню/фильтры/чипы)
  dup_lines       — сколько строк дублируется
  chars_per_num   — символов на одно число-с-единицей (плотность пользы)
  digit_recall    — доля «содержательных» чисел DOM, доживших до текста
  filter_leak     — сколько UI-чисел просочилось в текст (чем меньше, тем лучше)

Запуск:  docker exec auditlens-app python3 /tmp/context_bench.py [--json]
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/app/src")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

FIX = Path("/tmp/fixtures/pages")
NUM_RE = re.compile(r"\d[\d\s ]*(?:[.,]\d+)?\s*(?:%|₽|руб\w*|мес\w*|год\w*|лет|дн\w*)",
                    re.IGNORECASE)
# Контейнеры, которые по смыслу являются элементами управления, а не контентом.
UI_SEL = ("form", "select", "option", "label", "[class*=filter]", "[class*=facet]",
          "[role=tablist]", "[class*=tabs]", "[class*=chips]", "[class*=range]",
          "[class*=slider]", "nav", "aside")


def _norm_nums(text: str) -> Counter:
    """Мультимножество чисел-с-единицами (нормализованных)."""
    out = Counter()
    for m in NUM_RE.finditer(text or ""):
        s = re.sub(r"[\s ]", "", m.group(0)).lower()
        out[s] += 1
    return out


def dom_markup(html: str) -> tuple[Counter, Counter]:
    """(содержательные числа, UI-числа) по исходному DOM — автоматическая разметка."""
    from selectolax.parser import HTMLParser
    tree = HTMLParser(html)
    for sel in ("script", "style", "noscript", "svg"):
        for n in tree.css(sel):
            try:
                n.decompose()
            except Exception:
                pass
    ui = Counter()
    ui_tree = HTMLParser(html)
    for sel in UI_SEL:
        for n in ui_tree.css(sel):
            ui += _norm_nums(n.text(separator=" ") or "")
    allnums = _norm_nums(tree.body.text(separator=" ") if tree.body else "")
    content = Counter()
    for k, v in allnums.items():
        left = v - ui.get(k, 0)
        if left > 0:
            content[k] = left
    return content, ui


def bench_page(path: Path) -> dict:
    from bank_audit.rag.parsers.html_parser import parse_html
    html = path.read_text(encoding="utf-8", errors="replace")
    doc = parse_html(html.encode("utf-8"), url=f"file://{path.name}")
    text = doc.text or ""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    short = [l for l in lines if len(l) < 40]
    dups = sum(c - 1 for c in Counter(lines).values() if c > 1)
    out_nums = _norm_nums(text)
    content, ui = dom_markup(html)
    recovered = sum(min(v, out_nums.get(k, 0)) for k, v in content.items())
    leaked = sum(min(v, out_nums.get(k, 0)) for k, v in ui.items() if k not in content)
    n_out = sum(out_nums.values())
    return {
        "page": path.stem,
        "chars": len(text),
        "lines": len(lines),
        "short_lines_pct": round(len(short) * 100 / max(1, len(lines))),
        "dup_lines": dups,
        "nums_out": n_out,
        "chars_per_num": round(len(text) / max(1, n_out)),
        "dom_content_nums": sum(content.values()),
        "dom_ui_nums": sum(ui.values()),
        "digit_recall": round(recovered * 100 / max(1, sum(content.values()))),
        "filter_leak": leaked,
    }


def main() -> int:
    pages = sorted(p for p in FIX.glob("*.html"))
    man = {}
    mf = FIX / "manifest.json"
    if mf.exists():
        man = {m["name"]: m for m in json.loads(mf.read_text())}
    rows = []
    for p in pages:
        if man.get(p.stem, {}).get("stub"):
            continue
        try:
            rows.append(bench_page(p))
        except Exception as e:
            rows.append({"page": p.stem, "error": f"{type(e).__name__}: {str(e)[:60]}"})
    if "--json" in sys.argv:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    ok = [r for r in rows if "error" not in r]
    print(f"{'страница':24} {'симв':>7} {'кор.строк':>10} {'дубли':>6} "
          f"{'симв/число':>11} {'digit-recall':>13} {'утечка UI':>10}")
    print("─" * 92)
    for r in sorted(ok, key=lambda x: -x["chars"]):
        print(f"{r['page']:24} {r['chars']:>7} {str(r['short_lines_pct'])+'%':>10} "
              f"{r['dup_lines']:>6} {r['chars_per_num']:>11} "
              f"{str(r['digit_recall'])+'%':>13} {r['filter_leak']:>10}")
    if ok:
        n = len(ok)
        print("─" * 92)
        print(f"{'СРЕДНЕЕ':24} {sum(r['chars'] for r in ok)//n:>7} "
              f"{str(sum(r['short_lines_pct'] for r in ok)//n)+'%':>10} "
              f"{sum(r['dup_lines'] for r in ok)//n:>6} "
              f"{sum(r['chars_per_num'] for r in ok)//n:>11} "
              f"{str(sum(r['digit_recall'] for r in ok)//n)+'%':>13} "
              f"{sum(r['filter_leak'] for r in ok)//n:>10}")
    for r in rows:
        if "error" in r:
            print(f"  ✗ {r['page']}: {r['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
