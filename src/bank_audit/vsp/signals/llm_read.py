"""Модель читает отзывы о ВСП: что произошло, причастен ли сотрудник, серьёзность.

Слова-правила (risks.py) находят только известное и шумят; здесь каждый отзыв из
выборки читает модель и пишет нейтральное описание и поля — без закрытого списка
категорий. Узкие способы действия («почерки») потом собираются из описаний (motifs.py), так находятся и новые.

Прогон пакетами по BATCH отзывов, с учётом трат в рублях и потолком (CAP_RUB),
продолжается с места остановки: прочитанное (тот же отзыв, та же версия промпта,
тот же текст) не перечитывается. Результат — таблица vsp_review_llm.

Запуск: python -m bank_audit.vsp llm-read [--limit N] [--sample pilot]
Окружение: LLM_BASE_URL, LLM_API_KEY (как у приложения), VSP_DB_URL.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time

from sqlalchemy import Column, DateTime, Float, Integer, String, Table, Text, select

from ..models import utcnow
from ..storage import Store, meta, reviews
from .kinds import TYPED_SINCE

log = logging.getLogger(__name__)

# выбор по замеру на эталоне 05.11 (2 137 отзывов, ручная разметка): карточка — DeepSeek V4.1 Flash
# (полнота 90 %, точность 98 %, вина 90 %, дословная цитата 83 %, ≈0,2 ₽ за отзыв)
MODEL = os.getenv("VSP_LLM_MODEL", "deepseek-ai/DeepSeek-V4.1-Flash")
BATCH = int(os.getenv("VSP_LLM_BATCH", "5"))
INFLIGHT = int(os.getenv("VSP_LLM_INFLIGHT", "8"))
CAP_RUB = float(os.getenv("VSP_LLM_CAP_RUB", "4000"))
MIN_CHARS = int(os.getenv("VSP_LLM_MIN_CHARS", "60"))
# короткие жалобы тоже читаем: «Подключение услуг в тихую», «Девушка с краю сразу страховки впаривает»
# (06.11: 12 тыс. отзывов на 1–3★ короче 60 знаков не попадали к модели); короткие 4–5★ — нет
MIN_CHARS_NEG = int(os.getenv("VSP_LLM_MIN_CHARS_NEG", "15"))


def long_enough(text: str | None, rating) -> bool:
    n = len((text or "").strip())
    return n >= MIN_CHARS or (rating is not None and rating <= 3 and n >= MIN_CHARS_NEG)
PRICES = {  # ₽ за 1 млн токенов (вход, выход) — каталог Cloud.ru 05.11.2026
    "google/gemini-3-flash-preview": (85.4, 512.4), "google/gemini-3.8-flash": (133.5, 667.47),
    "google/gemini-3.5-flash-lite": (53.4, 444.98), "google/gemini-3.1-flash-lite": (42.7, 256.2),
    "deepseek-ai/DeepSeek-V4-Flash": (43.2978, 86.5834), "deepseek-ai/DeepSeek-V4.1-Flash": (64.94, 194.81),
    "deepseek-ai/DeepSeek-V4-Pro": (183.0, 732.0),
    "xiaomi/mimo-v2.6-flash": (24.92, 49.85), "xiaomi/mimo-v2.6-pro": (77.43, 154.85),
    "qwen/qwen3.7-flash": (5.34, 23.14), "qwen/qwen3.8-flash": (26.71, 83.66), "qwen/qwen3.7-plus": (71.2, 284.8),
    "openai/gpt-6-luna": (17.8, 89.0), "openai/gpt-5-nano": (9.821, 78.568), "openai/gpt-oss-120b": (15.86, 61.0),
    "z-ai/glm-5.3-flash": (26.71, 89.0), "z-ai/glm-5.3-flashx": (65.86, 222.49),
    "ai-sage/GigaChat3-10B-A1.8B": (12.2, 12.2), "ai-sage/GigaChat3.5-432B-A28B-Reasoning": (96.22, 288.6),
    "anthropic/claude-sonnet-5.5": (355.98, 1779.92),
}

review_llm = Table(
    "vsp_review_llm", meta,
    Column("review_id", String(160), primary_key=True),
    Column("prompt_version", String(20), primary_key=True),
    Column("text_hash", String(16)),
    Column("model", String(80)),
    Column("relevant", Integer),          # 1 — история, полезная аудиту ВСП
    Column("severity", Integer),          # 0–3
    Column("staff", String(20)),          # причастность сотрудника: нет | возможно | вероятно | явно
    Column("summary", Text),              # что произошло — нейтрально, одной-двумя фразами
    Column("scheme", Text),               # метка схемы своими словами
    Column("data_json", Text),            # все поля ответа
    Column("cost_rub", Float),
    Column("at", DateTime(timezone=True)),
)

PROMPT_VERSION = os.getenv("VSP_LLM_PROMPT", "v2")
SCREEN_VERSION = "s1"
# отбор — gpt-oss-120b: полнота 82 %, тяжёлые 98 %, точность 81 %, ≈0,009 ₽ за отзыв, отвечает на все
SCREEN_MODEL = os.getenv("VSP_LLM_SCREEN_MODEL", "openai/gpt-oss-120b")
TPM = int(os.getenv("VSP_LLM_TPM", "70000"))      # лимит Cloud.ru — 100 тыс. токенов в минуту на модель
SCREEN_BATCH = int(os.getenv("VSP_LLM_SCREEN_BATCH", "20"))
# этап 3 «уточнение» — только тяжёлые истории (тяжесть 2–3): тонкий тип, кому выгода, умысел, слова
# клиента о системности, чем закончилось, произошло ли в этом отделении. Замер 10.11 на 3 146 историях:
# дешёвая модель отбора ставит тип уверенно, но пропускает слова о системности, поэтому читает модель карточек.
DETAIL_VERSION = "d1"
DETAIL_MODEL = os.getenv("VSP_LLM_DETAIL_MODEL", MODEL)
DETAIL_BATCH = int(os.getenv("VSP_LLM_DETAIL_BATCH", "5"))
DETAIL_MIN_SEVERITY = 2
# уточняем истории не старше этой даты: ≈0,26 ₽ за историю, а для волн и очагов нужны последние годы;
# у более старых тип берётся из действий карточки (risk.kind_from_actions)
DETAIL_SINCE = TYPED_SINCE

# этап 1: дешёвая модель на всех текстах — только «есть ли эпизод» (полнота важнее точности)
SYSTEM_SCREEN = """Ты — аудитор розничного банка, читаешь отзывы клиентов об отделениях (ВСП) банка.
Для каждого отзыва ответь: есть ли в нём КОНКРЕТНЫЙ эпизод, полезный для проверки отделения: обман, оформление без
согласия или под видом другого, ложь об условиях или «обязательности», давление и наказание за отказ, доступ
сотрудника к телефону или аккаунтам клиента (Госуслуги, приложение, коды из СМС), касса и наличные (недостача, чужие
платежи), утечка данных, отказ принять жалобу или давление за жалобу, блат и «свои», посредники (риелторы, брокеры),
накрутка продаж, или другая подозрительная история про сотрудников.
Шум — НЕ эпизод: очереди, грубость без нарушения, некомпетентность без умысла, тарифы и комиссии банка, блокировки и
проверки по закону, банкоматы и приложение, звонки мошенников без связи с визитом, отзывы о банке в целом, помещение,
обычное предложение продуктов, если отказ приняли, спам и реклама. Оценка (звёзды) не важна.
Сомневаешься — отвечай true."""

SYSTEM_V2 = """Ты — аудитор розничного банка. Тебе дают отзывы клиентов об отделениях (ВСП) банка. Для каждого пойми,
есть ли в нём конкретный эпизод, полезный для проверки отделения: мошенничество или обман со стороны сотрудников,
оформление продуктов без согласия или под видом другого, ложь об условиях или об «обязательности», давление и наказание
за отказ, доступ сотрудника к телефону и аккаунтам клиента, нарушения с наличными и в кассе, утечки данных, отказ принять
жалобу или давление за жалобу, неформальные связи («свои» клиенты, посредники), накрутка продаж и очереди, а также любые
другие подозрительные истории, которые ты увидишь.

Важно:
1. Пиши нейтрально и только о том, что написано в отзыве. Не делай выводов о вине людей — это гипотеза для проверки.
   Не придумывай факты.
2. Оценка (звёзды) не важна: тяжёлый эпизод может быть в отзыве на 5 звёзд, а отзыв на 1 звезду — быть просто эмоцией.
3. НЕ считай эпизодом для аудита (это шум): очереди, ожидание, мало окон, сбои электронной очереди; грубость,
   высокомерие, разговоры по телефону — если нет отказа в услуге, отказа принять жалобу или давления; некомпетентность и
   ошибки без признаков умысла; комиссии и платные услуги по тарифу (справки, реквизиты, размен, переводы, обслуживание
   карты), даже если клиент называет это «развод» или «украли» — исключение: сотрудник сказал, что бесплатно, или сам
   выбрал платный вариант за клиента; блокировки и проверки по закону (115-ФЗ, антифрод, вопросы «не под давлением ли
   вы», заказ наличных заранее, требование документов); звонки внешних мошенников и взыскание долгов, если звонок не
   связан с визитом в отделение; отзывы о банке в целом, приложении, колл-центре, доставке, условиях продуктов;
   помещение, график, банкоматы, парковка, туалет; обычное предложение продуктов, если отказ клиента приняли; спам,
   реклама, политика, шутки.
4. Признаки, что виновато отделение или сотрудник, а не политика банка: горячая линия или другое отделение сказали
   иначе; действие без согласия или вопреки отказу; ложь об обязательности или условиях; явная выгода сотрудника или
   плана («помогите нам», «для плана»); сокрытие (удалили СМС, «подсунули среди документов», «сказали, что закрыли»).
   «Отделение», а не «сотрудник», — если клиент пишет, что так делают все или всегда, если участвовал или поддержал
   руководитель, если так же поступили с другими клиентами. «Политика банка» — если правило одинаково для всей сети.
5. Если эпизод произошёл в другом отделении, а этот отзыв о нём лишь упоминает, укажи это в other_vsp.
6. Подпись кодом из СМС, ссылка «для подтверждения», «оплатите 1 рубль», «подтвердите личность», работа с телефоном
   клиента, служебный аккаунт на телефоне клиента, смена паролей (Госуслуги, приложение) — важные признаки: отметь их
   в actions.
7. Цитата quote — дословный фрагмент отзыва до 300 символов, который лучше всего показывает эпизод. Не исправляй
   орфографию.

Серьёзность:
0 — эпизода для аудита нет (тогда заполни noise);
1 — слабый сигнал: навязывание без оформления, отказ принять жалобу, ошибка с ущербом без признаков умысла, оглашение
    суммы вслух, «свои» без очереди, похвала от посредника;
2 — значимый: продукт оформлен без согласия или под видом другого, ложь об обязательности или условиях с оформлением,
    наказание за отказ, доступ к телефону или аккаунтам клиента, деньги ушли не туда, звонки мошенников сразу после визита;
3 — тяжёлый: прямой ущерб и признаки умысла (чужие платежи деньгами клиента, недостача при выдаче, выдача пенсии только
    с покупкой, смена паролей или регистрация на Госуслугах без согласия, подделка подписи, ПИН-код у посторонних),
    особенно с уязвимым клиентом.

Для каждого отзыва верни объект:
{"relevant": true|false, "episode": "1–2 предложения: что произошло", "scheme_free": "3–8 слов: как бы ты назвал схему",
 "actions": ["оформление_без_согласия"|"подмена_продукта"|"ложь_об_обязательности"|"ложь_об_условиях"|"обусловливание"|
   "наказание_за_отказ"|"доступ_к_телефону_или_аккаунту"|"биометрия"|"касса_недостача_или_чужой_платеж"|
   "оглашение_или_утечка_данных"|"отказ_в_жалобе_или_давление"|"блат_очередь"|"посредник"|"накрутка_продаж"|
   "операционная_ошибка"|"навязывание_без_оформления"|"другое"],
 "realized": "оформлено"|"попытка"|"только предложено"|null,
 "products": ["кредитная карта"|"дебетовая карта"|"страхование"|"подписка"|"НПФ/ПДС/НСЖ"|"вклад"|"ипотека/сделка"|
   "потребкредит"|"наличные/касса"|"биометрия"|"Госуслуги/приложение"|"перевод/платёж"|"другое"],
 "employee_role": "операционист"|"консультант"|"кассир"|"кредитный специалист"|"ипотечный менеджер"|
   "менеджер Премьер/Первый"|"руководитель ВСП"|"охрана"|"неизвестно"|"не сотрудник",
 "employee_name": "как в тексте"|null,
 "vulnerability": ["пенсионер/пожилой"|"инвалид"|"иностранец"|"студент"|"родитель с младенцем"|
   "низкая цифровая грамотность"|"жертва мошенников"],
 "money_rub": число|null, "harm": ["деньги"|"доступ/данные"|"время"],
 "fault": "сотрудник"|"отделение"|"политика банка"|"третьи лица"|"неясно",
 "other_vsp": "адрес или номер другого отделения"|null,
 "source": "сам клиент"|"родственник"|"наблюдал со стороны"|"обобщение без случая",
 "severity": 0|1|2|3, "quote": "дословная цитата ≤300 символов",
 "noise": "очередь"|"грубость"|"некомпетентность"|"график"|"техника"|"тариф"|"блокировка/антифрод"|
   "внешние мошенники/взыскание"|"о банке в целом"|"помещение"|"похвала"|"спам"|null}

Примеры (сокращены):
«Пришла за картой, предложили участвовать в розыгрыше квартиры — нужна бесплатная карта. Дома в приложении оказалось,
что она кредитная… За соседним столом так же оформили двум пожилым» → relevant=true, actions=[подмена_продукта,
оформление_без_согласия], realized=оформлено, products=[кредитная карта], fault=отделение, severity=2.
«За распечатку реквизитов взяли 200 рублей! Грабёж!» → relevant=false, noise=тариф.
«На кассе сказали, что без паспорта не разменяют, и спросили, не звонил ли мне кто-то» → relevant=false,
noise=блокировка/антифрод.
«Сказали, что биометрия стоит 3299 руб. В чеке — sberzdorove korobki. На 900 сказали, что бесплатно» →
relevant=true, actions=[биометрия, ложь_об_условиях, оформление_без_согласия], products=[биометрия, подписка],
fault=сотрудник, severity=2.
Отвечай строго JSON."""
SYSTEM_V1 = SYSTEM_V2

SYSTEM_DETAIL = """Ты — аудитор розничного банка. Тебе дают отзывы клиентов об отделениях банка, в которых уже найден
эпизод для проверки (есть краткий пересказ). Уточни эпизод, чтобы находить массовые и системные злоупотребления
сотрудников. Пиши только то, что есть в отзыве; вину не утверждай; ничего не придумывай. Каждое поле заполняй по тексту
именно этого отзыва, не переноси значения с соседних. Читай отзыв до конца: слова о других клиентах, о повторе, о
руководителе и о том, чем всё закончилось, часто стоят в конце.

Для каждого отзыва верни объект с полями:
"kind": номер главного типа из списка ниже (одно число);
"kind2": номер второго типа или null;
"label": 3–7 слов — способ действия простыми словами, без имён, сумм, дат и городов, так чтобы один и тот же приём у
  разных клиентов назывался одинаково (например: «готовую кредитную карту вынесли вместе с дебетовой», «взяли телефон
  и подключили подписку», «биометрию назвали платной и продали пакет здоровья»);
"benefit": кому выгода по тексту: "сотрудник лично" (деньги или имущество клиента оказались у сотрудника или его
  знакомых, присвоение, откат) | "план продаж" (продукт оформлен ради плана, премии, показателей) | "третьи лица"
  (мошенники, посредники) | "нет" (ошибка, сбой, правило банка) | "неясно";
"intent": 0–3 — насколько по тексту похоже на умышленное действие сотрудника: 0 — не про сотрудника; 1 — скорее ошибка
  или грубая продажа; 2 — похоже на умысел (скрыл, сделал вопреки отказу, обманул); 3 — прямое присвоение, подделка,
  действия за клиента без его ведома;
"systemic": массив — прямые слова клиента о том, что случай не единичный: "другие клиенты" (видел или знает такие же
  случаи с другими людьми в этом отделении) | "повторно с нами" (с этим клиентом или его семьёй здесь так уже было) |
  "так заведено" (пишет, что так делают все или всегда; что руководитель знал, поддержал или сам участвовал);
  [] — если таких слов нет;
"outcome": чем закончилось по словам клиента: "вернули или исправили" | "банк признал нарушение" |
  "сотрудника наказали или уволили" | "отказали или не признали" | "не сообщается";
"escalation": массив из: "полиция" | "суд" | "ЦБ" | "прокуратура" | "жалоба в банк"; [] — если никуда не обращался
  или не сказано;
"here": true — событие произошло в отделении этой карточки (код и адрес даны перед отзывом); false — в другом месте
  (другое отделение, онлайн, колл-центр, банкомат на улице, доставка); null — неясно. Если клиент называет адрес или
  номер, совпадающие с отделением карточки, — это true;
"elsewhere": если here=false и клиент назвал другое отделение — его адрес или номер как в тексте, иначе null;
"window": окно, стол, касса или кабинет, если клиент назвал («окно 9»), иначе null;
"sum_kind": что за сумма названа в отзыве: "потеря" (клиент лишился этих денег) | "сумма продукта" (размер вклада,
  кредита, страховой премии, лимит карты) | "попытка" (пытались, но деньги не ушли) | null — суммы нет;
"event_date": когда это случилось, если клиент назвал (ГГГГ-ММ или ГГГГ), иначе null.

Типы (kind):
{kinds}

Примеры:
«Пригласили отца-пенсионера продлить вклад, вместо этого впарили страховку жизни на 300 тысяч на пять лет» → kind 6,
label «вместо продления вклада оформили страхование жизни», benefit "план продаж", intent 2, systemic [],
outcome "не сообщается", sum_kind "сумма продукта".
«Мать брала кредит, навязали страховку 35 тыс. Персонал во главе с управляющей отговаривал её от возврата, чуть ли не
угрожая» → kind 20, kind2 22, label «страховку включили в кредит и отговаривали от возврата», benefit "план продаж",
intent 2, systemic ["так заведено"], sum_kind "сумма продукта".
«Кассир недодала бабушке 5000 при выдаче пенсии, соседке месяц назад так же. Заявление писали — сказали, по камерам
всё верно» → kind 1, label «недодали при выдаче пенсии», benefit "неясно", intent 2, systemic ["другие клиенты"],
outcome "отказали или не признали", escalation ["жалоба в банк"], sum_kind "потеря".
«Постоянно тут проблемы: то платёж уйдёт не тому поставщику, в этот раз три платежа из пяти отправили не туда» →
kind 2, label «платежи отправили не тому получателю», benefit "нет", intent 1, systemic ["повторно с нами"].
«Менеджер в 9 окне взяла мой телефон „обновить приложение“, дома увидела заявку на кредитную карту и новый пароль.
После жалобы карту закрыли, сотрудницу, сказали, уволили» → kind 9, kind2 4, label «через телефон клиента подали
заявку на кредитную карту», benefit "план продаж", intent 3, outcome "сотрудника наказали или уволили",
escalation ["жалоба в банк"], window «окно 9».
Отвечай строго JSON."""


def system_detail() -> str:
    from .kinds import prompt_list
    return SYSTEM_DETAIL.replace("{kinds}", prompt_list())


FIELDS_DETAIL = ("kind", "kind2", "label", "benefit", "intent", "systemic", "outcome", "escalation", "here",
                 "elsewhere", "window", "sum_kind", "event_date")
_BENEFIT = {"сотрудник": "сотрудник лично", "план": "план продаж", "трет": "третьи лица", "нет": "нет"}
_SYSTEMIC = ("другие клиенты", "повторно с нами", "так заведено")
_OUTCOME = {"вернул": "вернули или исправили", "исправ": "вернули или исправили", "признал": "банк признал нарушение",
            "наказ": "сотрудника наказали или уволили", "уволи": "сотрудника наказали или уволили",
            "отказ": "отказали или не признали", "не признал": "отказали или не признали"}
_ESCALATION = ("полиция", "суд", "ЦБ", "прокуратура", "жалоба в банк")
_SUM_KIND = ("потеря", "сумма продукта", "попытка")


def normalize_detail(d: dict | None) -> dict | None:
    """Ответ этапа «уточнение» → поля. Значения вне списков приводятся к ближайшему или сбрасываются."""
    from .kinds import KINDS
    if not isinstance(d, dict) or "kind" not in d:
        return None

    def _kind(v):
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None
        return v if v in KINDS else None

    def _txt(v, n):
        v = str(v).strip() if v not in (None, "", "null", "None") else ""
        return v[:n] or None

    out: dict = {"kind": _kind(d.get("kind")) or 25, "kind2": _kind(d.get("kind2"))}
    if out["kind2"] == out["kind"]:
        out["kind2"] = None
    out["label"] = (_txt(d.get("label") or d.get("kind_free"), 120) or "").strip(" .«»\"").lower() or None
    b = str(d.get("benefit") or "").strip().lower()
    out["benefit"] = next((v for k, v in _BENEFIT.items() if b.startswith(k)), "неясно")
    try:
        out["intent"] = max(0, min(3, int(d.get("intent", d.get("staff_intent")) or 0)))
    except (TypeError, ValueError):
        out["intent"] = 0
    sy = d.get("systemic") if isinstance(d.get("systemic"), list) else []
    out["systemic"] = [x for x in _SYSTEMIC if any(str(y).strip().lower() == x for y in sy)]
    o = str(d.get("outcome") or "").strip().lower()
    out["outcome"] = next((v for k, v in _OUTCOME.items() if k in o), "не сообщается")
    es = d.get("escalation") if isinstance(d.get("escalation"), list) else []
    low = [str(y).strip().lower() for y in es]
    out["escalation"] = [x for x in _ESCALATION if x.lower() in low]
    h = d.get("here")
    out["here"] = h if isinstance(h, bool) else (True if str(h).lower() == "true" else False if str(h).lower() == "false"
                                                 else None)
    out["elsewhere"] = _txt(d.get("elsewhere"), 200) if out["here"] is False else None
    out["window"] = _txt(d.get("window"), 40)
    sk = str(d.get("sum_kind") or "").strip().lower()
    out["sum_kind"] = sk if sk in _SUM_KIND else None
    ed = str(d.get("event_date") or "").strip()
    out["event_date"] = ed if re.fullmatch(r"(19|20)\d{2}(-(0[1-9]|1[0-2]))?", ed) else None
    return out

FIELDS = ("relevant", "episode", "scheme_free", "actions", "realized", "products", "employee_role", "employee_name",
          "vulnerability", "money_rub", "harm", "fault", "other_vsp", "source", "severity", "quote", "noise")


def _hash(t: str) -> str:
    return hashlib.sha1((t or "").encode()).hexdigest()[:16]


def _loose(raw: str):
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0)) if m else None


def _where(x: dict) -> str:
    """Код, город и адрес отделения карточки. Без адреса модель записывала в «другое отделение»
    само это отделение (10.11: 108 из 593 таких историй) — и код выбрасывал историю."""
    if not x.get("vsp"):
        return ""
    parts = [str(x["vsp"]), str(x.get("city") or "")]
    addr = re.sub(r"^\d{6},?\s*", "", str(x.get("address") or "")).strip()
    if addr:
        parts.append(addr[:160])
    return " Отделение карточки: " + ", ".join(p for p in parts if p) + "."


def card(x: dict, n: int) -> str:
    return f"[{n}] Оценка {x['rating']}★, {str(x['date'])[:10]}.{_where(x)}\nОтзыв:\n<<<{(x['text'] or '')[:2500]}>>>"


def card_detail(x: dict, n: int) -> str:
    return (f"[{n}] Оценка {x['rating']}★, {str(x['date'])[:10]}.{_where(x)}\nПересказ: {(x.get('episode') or '')[:500]}"
            f"\nОтзыв:\n<<<{(x['text'] or '')[:2500]}>>>")


def quote_ok(q: str, text: str) -> bool:
    """Цитата дословно есть в тексте (с точностью до пробелов) — модели «причёсывают» цитаты.
    Модель часто склеивает цитату из кусков через «...»: тогда дословно должен быть каждый
    кусок (замер 05.11 на проде: 8 из 20 цитат были такими и ложно считались пересказом)."""
    n = lambda z: re.sub(r"\s+", " ", (z or "").strip(" .…\"«»“”„")).lower().replace("ё", "е")
    if not q:
        return False
    nt = n(text)
    parts = [n(p) for p in re.split(r"\s*(?:\.{3}|…)\s*", q)]
    parts = [p for p in parts if len(p) >= 8]
    if len(parts) > 1:
        return all(p[:120] in nt for p in parts)
    return n(q)[:120] in nt


def _b(v) -> int:
    return 1 if str(v).strip().lower() in ("1", "true", "да", "yes") else 0


def normalize(d: dict | None) -> dict | None:
    """Ответ этапа 2 → поля. Сводка в старые колонки: summary=episode, scheme=scheme_free, staff=fault."""
    if not isinstance(d, dict) or "relevant" not in d:
        return None
    out = {k: d.get(k) for k in FIELDS}
    out["relevant"] = _b(out["relevant"])
    try:
        out["severity"] = max(0, min(3, int(out.get("severity") or 0)))
    except (TypeError, ValueError):
        out["severity"] = 0
    if not out["relevant"]:
        out["severity"] = 0
    for k in ("episode", "scheme_free", "employee_role", "fault", "realized", "source", "noise", "quote", "other_vsp",
              "employee_name"):
        out[k] = str(out.get(k) or "")[:700] if out.get(k) is not None else None
    for k in ("actions", "products", "vulnerability", "harm"):
        v = out.get(k)
        out[k] = [str(x)[:60] for x in v][:8] if isinstance(v, list) else []
    try:
        out["money_rub"] = float(out["money_rub"]) if out.get("money_rub") not in (None, "", "null") else None
    except (TypeError, ValueError):
        out["money_rub"] = None
    return out


def normalize_screen(d) -> dict | None:
    if not isinstance(d, dict) or "episode" not in d:
        return None
    return {"relevant": _b(d.get("episode")), "why": str(d.get("why") or "")[:200]}


class Reader:
    """Клиент модели: повторы, отказ от неподдержанных параметров (temperature, отключение
    рассуждений), учёт рублей по токенам, включая скрытые рассуждения модели."""

    def __init__(self, model: str = MODEL, system: str = SYSTEM_V2, *, think: bool = True, tpm: int | None = None):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"],
                                  timeout=300, max_retries=0)
        self.model, self.system = model, system
        self.think = think                                     # False — просим модель не рассуждать (дешевле и быстрее)
        self.tpm = tpm or TPM
        self.sem = asyncio.Semaphore(INFLIGHT)
        self.rub = 0.0
        self.calls = self.errors = 0
        self.drop: set[str] = set()
        self.no_temp = False
        self._window: list[tuple[float, int]] = []          # (время, токены) за последнюю минуту

    async def _throttle(self) -> None:
        """Держим расход ниже лимита токенов в минуту: иначе 429 и мешаем ИИ-помощнику."""
        while True:
            now = time.monotonic()
            self._window = [(t, n) for t, n in self._window if now - t < 60]
            if sum(n for _, n in self._window) < self.tpm:
                return
            await asyncio.sleep(max(0.5, 60 - (now - self._window[0][0])))

    def _cost(self, tin: int, tout: int) -> float:
        pin, pout = PRICES.get(self.model, (100.0, 500.0))
        return tin / 1e6 * pin + tout / 1e6 * pout

    def _extras(self) -> dict:
        ex: dict = {"temperature": 0}
        if self.model.startswith(("qwen/", "z-ai/")) or not self.think:   # без рассуждений: дешевле и быстрее
            ex["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        if self.model.startswith(("openai/gpt-5", "openai/gpt-6")):
            ex["reasoning_effort"] = "low"
        return {k: v for k, v in ex.items() if k not in self.drop}

    async def ask(self, user: str) -> tuple[object, float]:
        for attempt in range(5):
            kw = dict(model=self.model, max_tokens=8000,
                      messages=[{"role": "system", "content": self.system}, {"role": "user", "content": user}],
                      **self._extras())
            try:
                async with self.sem:
                    await self._throttle()
                    r = await self.client.chat.completions.create(**kw)
                u = r.usage
                self._window.append((time.monotonic(), int(u.prompt_tokens or 0) + int(u.completion_tokens or 0)))
                rub = self._cost(int(u.prompt_tokens or 0), int(u.completion_tokens or 0))
                self.rub += rub
                self.calls += 1
                try:
                    return _loose(r.choices[0].message.content), rub
                except Exception:  # noqa: BLE001
                    return None, rub
            except Exception as e:  # noqa: BLE001
                low = str(e).lower()
                hit = [k for k in ("temperature", "reasoning_effort") if k.split("_")[0] in low and k not in self.drop]
                if "enable_thinking" in low or "chat_template" in low:
                    hit.append("extra_body")
                if hit:
                    self.drop.update(hit)
                    continue
                self.errors += 1
                if getattr(e, "status_code", None) in (400, 401, 403, 404, 422):
                    break
                await asyncio.sleep(min(60, 4 * 2 ** attempt))
        return None, 0.0

    async def read_batch(self, items: list[dict], *, tail: str, norm, fmt=None) -> dict[str, dict]:
        fmt = fmt or card
        user = "Прочитай каждый отзыв отдельно.\n\n" + "\n\n".join(fmt(x, k + 1) for k, x in enumerate(items)) + tail
        d, rub = await self.ask(user)
        arr = [it for it in ((d or {}).get("items") or []) if isinstance(it, dict)] if isinstance(d, dict) else []
        by_n = {int(it["n"]): it for it in arr if str(it.get("n", "")).isdigit()}
        out = {}
        share = rub / max(1, len(items))
        for k, x in enumerate(items):
            v = norm(by_n.get(k + 1))
            if v is None:                                  # пропустила — по одному
                d1, rub1 = await self.ask(fmt(x, 1) + tail)
                if isinstance(d1, dict) and isinstance(d1.get("items"), list) and d1["items"]:
                    d1 = d1["items"][0]
                v = norm(d1)
                share += rub1
            if v is not None:
                v["_cost"] = share
                out[x["review_id"]] = v
        return out


TAIL_SCREEN = '\n\nВерни JSON: {"items":[{"n":1,"episode":true|false,"why":"≤10 слов"}, ...]} — по объекту на каждый отзыв.'
TAIL_CARD = ('\n\nВерни JSON: {"items":[{"n":1, ...поля по схеме...}, ...]} — по объекту на каждый отзыв, '
             'поле n обязательно.')
TAIL_DETAIL = TAIL_CARD
SPAM_AUTHOR_MIN = int(os.getenv("VSP_LLM_SPAM_AUTHOR", "40"))   # столько отзывов о Сбере у одного аккаунта — рассылка


def _norm_text(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").lower()).strip()


def candidates(store: Store, *, stage: str, ids: list[str] | None = None) -> list[dict]:
    """Отзывы для этапа. Одинаковые тексты — один раз (остальные получат тот же ответ),
    массовые аккаунты (политика, рассылка одной жалобы) — пропускаются.
    Этап detail — только отзывы с карточкой тяжести 2–3; к ним прикладывается пересказ.
    Уже прочитанное отсеивает база: шаг сборщика идёт каждый час и не должен тянуть все тексты."""
    from datetime import datetime, timezone

    from sqlalchemy import exists, func

    from ..storage import branches
    meta.create_all(store.engine, tables=[review_llm])
    version = {"screen": SCREEN_VERSION, "detail": DETAIL_VERSION}.get(stage, PROMPT_VERSION)

    def has(ver: str, *conds):
        t = review_llm.alias()
        return exists().where(t.c.review_id == reviews.c.review_id, t.c.prompt_version == ver,
                              *(cond(t) for cond in conds))

    q = select(
        reviews.c.review_id, reviews.c.rating, reviews.c.review_date.label("date"),
        reviews.c.review_text.label("text"), reviews.c.author_ref, branches.c.name.label("vsp"), branches.c.city,
        branches.c.address
    ).outerjoin(branches, branches.c.branch_id == reviews.c.branch_id).where(
        reviews.c.review_text.isnot(None), ~has(version))
    if ids is not None and len(ids) <= 5000:
        q = q.where(reviews.c.review_id.in_(list(ids)))
    if ids is None and stage == "card":
        q = q.where(has(SCREEN_VERSION, lambda t: t.c.relevant == 1))
    if ids is None and stage == "detail":
        q = q.where(has(PROMPT_VERSION, lambda t: t.c.relevant == 1, lambda t: t.c.severity >= DETAIL_MIN_SEVERITY),
                    reviews.c.review_date >= datetime.fromisoformat(DETAIL_SINCE).replace(tzinfo=timezone.utc))
    with store.engine.connect() as c:
        episodes: dict[str, str] = {}
        if stage == "detail":
            episodes = {r[0]: r[1] or "" for r in c.execute(select(review_llm.c.review_id, review_llm.c.summary).where(
                review_llm.c.prompt_version == PROMPT_VERSION, review_llm.c.relevant == 1,
                review_llm.c.severity >= DETAIL_MIN_SEVERITY))}
        spam = set()
        if stage != "detail":
            spam = {r[0] for r in c.execute(select(reviews.c.author_ref).where(
                reviews.c.author_ref.isnot(None), reviews.c.review_text.isnot(None)).group_by(reviews.c.author_ref)
                .having(func.count() >= SPAM_AUTHOR_MIN))}
        rows = [dict(r) for r in c.execute(q).mappings()]
    want = set(ids) if ids is not None else None
    out, seen = [], {}
    for r in rows:
        if want is not None and r["review_id"] not in want:
            continue
        if want is None and stage != "detail" and (not long_enough(r["text"], r["rating"])
                                                   or r["author_ref"] in spam):
            continue
        if stage == "detail":
            r["episode"] = episodes.get(r["review_id"], "")
            # копии одного текста у разных отделений читаем отдельно: «произошло здесь» у них разное
            h = _hash(_norm_text(r["text"]) + "|" + str(r["vsp"] or ""))
        else:
            h = _hash(_norm_text(r["text"]))
        if h in seen:
            seen[h]["_dups"].append(r["review_id"])
            continue
        r["_dups"] = []
        seen[h] = r
        out.append(r)
    if stage == "detail":
        out.sort(key=lambda r: str(r["date"] or ""), reverse=True)      # свежие истории — первыми
    else:
        out.sort(key=lambda r: (r["rating"] or 9, -len(r["text"] or "")))
    return out


async def run_stage(store: Store, stage: str, *, limit: int = 1000, ids: list[str] | None = None,
                    model: str | None = None, cap_rub: float | None = None) -> dict:
    found = candidates(store, stage=stage, ids=ids)
    items = found[:limit]
    screen, detail = stage == "screen", stage == "detail"
    if screen:
        rd = Reader(model or SCREEN_MODEL, SYSTEM_SCREEN)
        size, tail, norm, fmt, version = SCREEN_BATCH, TAIL_SCREEN, normalize_screen, card, SCREEN_VERSION
    elif detail:
        rd = Reader(model or DETAIL_MODEL, system_detail())
        size, tail, norm, fmt, version = DETAIL_BATCH, TAIL_DETAIL, normalize_detail, card_detail, DETAIL_VERSION
    else:
        rd = Reader(model or MODEL, SYSTEM_V2)
        size, tail, norm, fmt, version = BATCH, TAIL_CARD, normalize, card, PROMPT_VERSION
    cap = CAP_RUB if cap_rub is None else cap_rub
    t0 = time.monotonic()
    saved = 0

    async def one(b):
        nonlocal saved
        if rd.rub >= cap:
            return
        got = await rd.read_batch(b, tail=tail, norm=norm, fmt=fmt)
        rows = []
        for x in b:
            v = got.get(x["review_id"])
            if not v:
                continue
            if screen:
                core = {"relevant": v["relevant"], "severity": None, "staff": None, "summary": v["why"], "scheme": None}
                data = {"why": v["why"]}
            elif detail:
                data = {k: v[k] for k in FIELDS_DETAIL}
                core = {"relevant": 1, "severity": None, "staff": v["benefit"], "summary": v["label"],
                        "scheme": str(v["kind"])}
            else:
                data = {k: v[k] for k in FIELDS}
                data["quote_ok"] = quote_ok(v.get("quote") or "", x["text"])
                core = {"relevant": v["relevant"], "severity": v["severity"], "staff": v.get("fault"),
                        "summary": v.get("episode"), "scheme": v.get("scheme_free")}
            for rid in [x["review_id"], *x["_dups"]]:
                rows.append({"review_id": rid, "prompt_version": version, "text_hash": _hash(x["text"]),
                             "model": rd.model, **core, "data_json": json.dumps(data, ensure_ascii=False),
                             "cost_rub": round(v["_cost"], 5) if rid == x["review_id"] else 0.0, "at": utcnow()})
        if rows:
            with store.engine.begin() as c:
                for r in rows:
                    c.execute(review_llm.delete().where(review_llm.c.review_id == r["review_id"],
                                                        review_llm.c.prompt_version == version))
                c.execute(review_llm.insert(), rows)
            saved += len(rows)
            if saved // 2000 != (saved - len(rows)) // 2000 or (detail and saved // 300 != (saved - len(rows)) // 300):
                log.info("llm-read %s: %d отзывов, %.1f ₽", stage, saved, rd.rub)
    await asyncio.gather(*(one(items[i:i + size]) for i in range(0, len(items), size)))
    res = {"stage": stage, "read_unique": len(items), "waiting": len(found), "saved": saved, "rub": round(rd.rub, 2),
           "calls": rd.calls,
           "errors": rd.errors, "minutes": round((time.monotonic() - t0) / 60, 1), "model": rd.model,
           "version": version, "dropped_params": sorted(rd.drop)}
    log.info("llm-read: %s", res)
    return res


async def run(store: Store, *, limit: int = 1000, sample: str | None = None, ids: list[str] | None = None,
              stage: str = "both", cap_rub: float | None = None) -> dict:
    """stage: screen | card | detail | both (отбор и карточка) | all (все три этапа подряд)."""
    out = {}
    left = cap_rub

    def spent(res: dict) -> None:
        nonlocal left
        if left is not None:
            left = max(0.0, left - float(res.get("rub") or 0))

    if stage in ("screen", "both", "all"):
        out["screen"] = await run_stage(store, "screen", limit=limit, ids=ids, cap_rub=left)
        spent(out["screen"])
    if stage in ("card", "both", "all"):
        out["card"] = await run_stage(store, "card", limit=limit, ids=ids if stage == "card" else None, cap_rub=left)
        spent(out["card"])
    if stage in ("detail", "all"):
        out["detail"] = await run_stage(store, "detail", limit=limit, ids=ids if stage == "detail" else None,
                                        cap_rub=left)
    return out


def unread(store: Store) -> dict:
    """Сколько подходящих отзывов ждёт каждого этапа — для сторожа и строки в интерфейсе."""
    return {st: len(candidates(store, stage=st)) for st in ("screen", "card", "detail")}
