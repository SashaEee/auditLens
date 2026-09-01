"""Этап 3: доступность ссылок и датировка источников.

Три жалобы аудиторов сводятся к одному: аудитор не может
отличить живой свежий источник от мёртвого или прошлогоднего.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

ok = fail = 0
def check(name, cond):
    global ok, fail
    if cond: ok += 1;  print(f"  ✓ {name}")
    else:    fail += 1; print(f"  ✗ {name}")

print("\n— дата публикации из разметки —")
from bank_audit.digest.news import date_from_html
cases = [
    ('<meta property="article:published_time" content="2025-03-14T10:00:00+03:00">', "2025-03-14"),
    ('{"@type":"NewsArticle","datePublished":"2024-11-02T08:15:00Z"}', "2024-11-02"),
    ('<time datetime="2026-01-09">9 января</time>', "2026-01-09"),
]
for html, want in cases:
    got = date_from_html(html)
    check(f"{want} извлечена", got is not None and got.date().isoformat() == want)
check("без метаданных — None", date_from_html("<p>в 2024 году ЦБ ввёл</p>") is None)
check("пустой вход не падает", date_from_html("") is None)

print("\n— дата не выдумывается из года в тексте —")
check("голый год не дата",
      date_from_html("<html><body>ставка выросла в 2023</body></html>") is None)

print("\n— проставление дат фактам —")
from bank_audit.research.gptr import runstate, reviews as R, facts as F
st = runstate.new_run()
st.page_dates["https://a.ru/x"] = "2025-06-01"
st.review_meta["https://banki.ru/r/1"] = {"date": "2024-02-03T00:00:00", "bank": "sber"}
reg = F.FactRegistry()
f1 = F.Fact(id="f1", subject="Банк", attribute="ставка", value="10", unit="%",
            verbatim="ставка 10%", url="https://a.ru/x", stance="declared")
f2 = F.Fact(id="f2", subject="Банк", attribute="жалоба", value="", unit="",
            verbatim="не дозвонился", url="https://banki.ru/r/1", stance="observed")
f3 = F.Fact(id="f3", subject="Банк", attribute="срок", value="5", unit="дн",
            verbatim="5 дней", url="https://no-date.ru/", stance="declared")
reg.facts.extend([f1, f2, f3])
n = R.stamp_dates(reg)
check("веб-странице проставлена дата разметки", f1.date == "2025-06-01")
check("отзыву — дата из корпуса", f2.date == "2024-02-03")
check("без даты остаётся пустым", f3.date == "")
check("посчитаны обе", n == 2)

f1.date = "2020-01-01"
R.stamp_dates(reg)
check("уже проставленную не перетираем", f1.date == "2020-01-01")

print("\n— проверка живости ссылок —")
check("пустой список не ходит в сеть", R.check_alive([]) == set())
unreach = R.check_alive(["https://this-host-does-not-exist-al.invalid/x"], timeout=4)
check("недоступность сети НЕ объявляет ссылку мёртвой", unreach == set())
# Живая проверка — на домене корпуса отзывов, ради которого всё и делалось.
# Если сети нет (машина за VPN), проверку пропускаем, а не заваливаем.
LIVE = "https://www.banki.ru/services/responses/"
DEADU = "https://www.banki.ru/services/responses/bank/response/00000000/"
import httpx
try:
    with httpx.Client(timeout=10, follow_redirects=True) as c:
        c.get(LIVE)
    net = True
except Exception:
    net = False
if net:
    d = R.check_alive([DEADU, LIVE], timeout=10)
    check("несуществующий отзыв признан мёртвым", DEADU in d)
    check("живая страница не помечена", LIVE not in d)
else:
    print("  · сеть недоступна — живые проверки пропущены")

print("\n— доступность новостного источника —")
from bank_audit.digest.writer import _reach_of
check("telegram помечен", _reach_of("https://t.me/cbr", {}) == "telegram")
check("прочитанный — ok", _reach_of("https://a.ru/n", {"https://a.ru/n": "текст"}) == "ok")
check("пробовали и не смогли — unreachable",
      _reach_of("https://a.ru/n", {"https://a.ru/n": ""}) == "unreachable")
check("НЕ пробовали — молчим, а не обвиняем",
      _reach_of("https://a.ru/n", {"https://b.ru/x": "т"}) == "unknown")
check("без словаря тел — unknown", _reach_of("https://a.ru/n", None) == "unknown")
check("пустой url не падает", _reach_of(None, None) == "unknown")

print(f"\nитого: {ok} ок, {fail} с ошибкой")
sys.exit(1 if fail else 0)
