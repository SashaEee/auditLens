"""Загрузка эталона срезов «Аудита отзывов» в review_slice_gold — для канарейки
качества (rag/review_slices_canary.py, миграция 107).

ЗАЧЕМ. Эталон размечают люди вне приложения: файл решений по срезам
({"gold": {id: {срез: {"v": bool, "clause": …}}}}) и файл выборки с адресами жалоб
(список {id, url, …} или {id: url}). Канарейке нужны только адрес и решения; тексты
она каждый раз читает из хранилища отзывов, как разметка. Сами файлы в репозиторий
не кладём — в них тексты жалоб.

Повторная загрузка того же набора обновляет метки (upsert по адресу), не множит строки.

  .venv/bin/python scripts/load_slice_gold.py <решения.json> <выборка.json>
  .venv/bin/python scripts/load_slice_gold.py <решения.json> <выборка.json> --source <имя> --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bank_audit.rag import review_slices_canary as C  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Эталон срезов → review_slice_gold")
    ap.add_argument("gold", help="решения по срезам (JSON)")
    ap.add_argument("sample", help="выборка с адресами жалоб (JSON)")
    ap.add_argument("--source", help="имя набора разметки (по умолчанию — имя файла решений)")
    ap.add_argument("--dry-run", action="store_true", help="только разобрать и показать счёт")
    a = ap.parse_args(argv)
    gold = json.loads(Path(a.gold).read_text(encoding="utf-8"))
    sample = json.loads(Path(a.sample).read_text(encoding="utf-8"))
    rows, st = C.parse_gold(gold, sample)
    pos = ", ".join(f"{k} {v}" for k, v in st["positives"].items())
    print(f"эталон: {st['ids']} жалоб, с адресом и метками: {st['rows']}, "
          f"без адреса или меток: {len(st['no_url'])}, повторов адреса: {st['duplicates']}")
    print(f"положительных по срезам: {pos}; без единого среза: {st['negatives']}")
    if st["unknown_keys"]:
        print(f"срезы не из реестра пропущены: {', '.join(st['unknown_keys'])}")
    if a.dry_run:
        return 0
    n = C.save_gold(rows, a.source or Path(a.gold).stem)
    db = C.gold_stats()
    print(f"записано: {n}; в таблице: {db['rows']}, из них в индексе отзывов: {db['in_index']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
