"""Замечания этапа 8: период в «Отзывах», достоверность позиции, полный текст документа."""
import pathlib, re, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = (ROOT / "src/bank_audit/web/app.py").read_text()
RD  = (ROOT / "src/bank_audit/rag/reviews_dash.py").read_text()
JSX = (ROOT / "src/bank_audit/web/static/app.jsx").read_text()
JS  = (ROOT / "src/bank_audit/web/static/app.js").read_text()

ok = fail = 0
def check(name, cond):
    global ok, fail
    if cond: ok += 1
    else:
        fail += 1
        print("  ПРОВАЛ:", name)

print("период во вкладке «Отзывы» сквозной")
check("темы принимают период", "def reviews_themes(bank: str = \"Сбербанк\", product: Optional[str] = None,\n                   days: int = 90)" in APP)
check("география принимает период", re.search(r"def reviews_geo\([^)]*days: int = 365", APP, re.S) is not None)
check("продукты принимают период", "def reviews_products(bank: str = \"Сбербанк\", days: int = 365)" in APP)
check("темы получают период с фронта", "/api/reviews/themes?bank=${enc(bank)}${pq()}&days=${days}" in JSX)
check("география получает период с фронта", "/api/reviews/geo?bank=${enc(bank)}${pq()}&days=${days}" in JSX)
check("период попал в собранный фронт", "/api/reviews/themes?bank=" in JS and "&days=" in JS)

print("темы считаются по выбранному окну, а не по зашитому")
check("окно тем параметризовано", ":d2" in RD and "make_interval(days => :d)" in RD)
check("разметочная ветка знает период", "def _themes_from_labels(bc: str, product: str | None, days: int = 90)" in RD)
check("период передан в разметочную ветку", "_themes_from_labels(bc, product, days)" in RD)
check("кэш тем разделён по периоду", 'f"th:{bc}:{product}:{days}"' in RD)
check("ответ тем сообщает реальный период", '"days": days, "total": total' in RD)
check("запасная ветка тоже по периоду", 'params["_d"], params["_d2"] = days, days * 2' in RD)
check("зашитого окна 90 в темах не осталось",
      "make_interval(days => 90)" not in RD.split("def themes(")[0].split("def _themes_from_labels")[1])

print("даты из будущего не попадают в темы")
check("верхняя граница в разметочной ветке", RD.count("f.dt <= now()") >= 2)
check("верхняя граница в запасной ветке", 'r."datePublished" <= now()' in RD)

print("позиция на рынке считается по достоверным числам")
check("атлас читает сторожа правдоподобия", "qf.reason                        AS implausible_reason" in APP)
check("атлас джойнит quality_flag",
      APP.count("FROM quality_flag q2") >= 2)
check("неправдоподобное не идёт в распределение", 'if r.get("implausible_reason"):' in APP)
check("отсев виден в паспорте выборки", '"implausible_excluded": implausible.get(cid, 0),' in APP)
check("отсев доезжает до вердикта", '"implausible_excluded": c.get("implausible_excluded", 0),' in APP)

print("банк в позиции — один, а не два под разными слагами")
check("ключ по очищенному имени", "def bkey(row) -> str:" in APP)
check("ключ применён в подсчёте банков", "seen_banks.setdefault(r[\"category\"], set()).add(bkey(r))" in APP)
check("ключ применён к лучшему офферу", "best[bkey(r)] = {" in APP and "cur = best.get(bkey(r))" in APP)
check("слаг остался в payload", '"slug": r["bank_slug"], "name": r["bank_name"]' in APP)

print("вид источника называется одинаково с обеих сторон")
check("словарь знает regulatory", "regulatory:    \"Регулятор\"," in JSX)
check("словарь знает отзывы и новости", 'review:        "Отзывы",' in JSX and 'news:          "Новости",' in JSX)
check("счётчик официальных считает regulatory", 's.source_kind==="regulatory"' in JSX)

print("документ открывается целиком")
check("эндпоинт полного текста есть", '@app.get("/api/knowledge/doc/{document_id}/text")' in APP)
check("текст отдаётся порциями", "DOC_TEXT_PAGE = 60_000" in APP and '"next_offset"' in APP)
check("подгрузка не теряет начало", 'substr(content_text, :off, :lim)' in APP)
check("кнопка «показать целиком» есть", "Показать текст целиком" in JSX)
check("видно, сколько показано из скольких", "показано ${(full.offset+full.text.length)" in JSX)
check("кнопка попала в сборку", "knowledge/doc/" in JS and "/text?offset=" in JS)

print("динамика подписана честно")
check("подпись про 14 месяцев", "переключатель периода на неё не влияет" in JSX)
check("подписи тем следуют периоду", "жалоб за ${th.days||days} дн" in JSX)

print("клик по городу уважает период")
check("город передаёт период", "&city=${enc(value)}&days=${days}" in JSX)

print("карта покрытия: «не собираем» отличимо от «нет на рынке»")
import sys as _s
_s.path.insert(0, str(ROOT / "src"))
from bank_audit import categories as _cat            # noqa: E402
from bank_audit.ai.hermes_quick import needs_legal_note  # noqa: E402
check("карта покрытия развёрнута", len(_cat.NOT_COVERED) >= 15)
check("категории витрины не попали в непокрытые", not (set(_cat.NOT_COVERED) & set(_cat.CAT_IDS)))
check("у каждой записи есть причина и статус",
      all(n.get("reason") and n.get("status") and n.get("label")
          for n in _cat.NOT_COVERED.values()))
check("прежнее имя RETIRED работает", _cat.RETIRED.get("metals"))
check("есть функция объяснения", _cat.coverage_note("insurance") is not None
      and _cat.coverage_note("deposit") is None)
check("карта отдаётся наружу", '@app.get("/api/meta/coverage")' in APP)
check("витрина показывает, чего нет", "Чего в витрине нет" in JSX and "mk-cov" in JS)
ANALYST = (ROOT / "src/bank_audit/ai/analyst.py").read_text()
check("быстрый режим не обещает несобираемое", '"metals", "other"' not in ANALYST)
check("пустая выборка объясняется", '"not_collected": bool(note)' in ANALYST)
check("правило различать в промпте", "РАЗЛИЧАЙ «мы этого не собираем»" in ANALYST)

print("быстрый режим: заземление и страховки")
HQ = (ROOT / "src/bank_audit/ai/hermes_quick.py").read_text()
check("правила передаются с заданием", "QUICK_RULES" in HQ and "_with_rules(question)" in HQ)
check("правила отключаемы", 'os.getenv("QUICK_RULES"' in HQ)
check("ответ обязателен", "Ответ обязателен всегда" in HQ)
check("пустой прогон уходит в откат",
      "hermes завершил прогон без ответа" in HQ)
check("ссылка на НПА помечается", "Ссылки на нормативные акты проверьте" in HQ)
for t, want in [("по ФЗ № 273-ФЗ требования", True),
                ("Приказ № 117-Э от 28 февраля", True),
                ("согласно статье 51 № 273", True),
                ("ставка 19%, медиана 20%", False),
                ("Сбер 4 место из 139 банков", False)]:
    check(f"НПА в тексте: {t[:28]}", needs_legal_note(t) is want)

print("цвет объекта аудита")
IDX = (ROOT / "src/bank_audit/web/static/index.html").read_text()
check("токен Сбера объявлен в обеих темах", IDX.count("--sber:") == 2)
check("точка Сбера больше не красная", ".mk-dot.sber{width:9px;height:9px;background:var(--sber)" in IDX)
check("подпись объекта аудита перекрашена", 'fontSize:10,color:"var(--sber)"' in JSX)
check("цвет попал в сборку", "var(--sber)" in JS)

print(f"\n{ok} проверок пройдено, провалов: {fail}")
sys.exit(1 if fail else 0)
