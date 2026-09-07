"""Проверка изменений на то, что не должно уезжать в публичный репозиторий.

Аббревиатуры банков ищем только как самостоятельные слова: «МБ» после числа —
это мегабайты, и раньше скан спотыкался именно об это.
"""
import pathlib
import re
import subprocess
import sys

PATTERNS = [
    r"ecs-oarb", r"\boarb\b", r"ОАИТ", r"Authentik", r"87\.242\.123\.218",
    r"amzenkovskiy", r"uva-advanced", r"id_ed25519", r"\bК4\b",
    r"территориальн\w+ банк", r"свод обратной связи", r"foundation-models\.api",
    # Аббревиатуры территориальных банков — только не после числа и не в
    # единицах измерения: «2 МБ» и «3,4 МБ» это не банк.
    r"(?<![\d,.]\s)(?<![\d,.])\b(ЦЧБ|ВВБ|СРБ|ББ|МБ)\b(?!/с)",
]
BAD = re.compile("|".join(PATTERNS), re.I)
TEXT_SUFFIX = {".py", ".md", ".jsx", ".js", ".html", ".sql", ".toml", ".txt", ".yml", ".yaml"}


SELF = "scripts/_leak_scan.py"    # в самом сканере запретные слова перечислены по делу


def added_lines() -> list[tuple[str, str]]:
    """Строки, добавленные в рабочем дереве и в новых файлах."""
    out = []
    diff = subprocess.run(["git", "diff", "-U0", "HEAD"], capture_output=True, text=True).stdout
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((path, line[1:]))
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                               capture_output=True, text=True).stdout.split()
    for f in untracked:
        p = pathlib.Path(f)
        if p.suffix in TEXT_SUFFIX and p.is_file():
            out += [(f, l) for l in p.read_text(encoding="utf-8", errors="ignore").splitlines()]
    return out


def main() -> int:
    hits = [(p, l) for p, l in added_lines() if p != SELF and BAD.search(l)]
    for p, l in hits[:20]:
        print(f"  {p}: {l.strip()[:120]}")
    print(f"утечек: {len(hits)}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
