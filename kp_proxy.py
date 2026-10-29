#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KP-Proxy — «мозг» сервиса автоматизации коммерческих предложений (КП) для СЭС.
Один файл, стандартная библиотека Python 3 (без зависимостей).

Отвечает за:
  - живой каталог CRM KeepinCRM (только оборудование в наличии, цены в грн по курсу),
  - автоподбор оборудования по выбору менеджера (готовая станция ИЛИ мощности),
  - калькулятор экономии (Власне використання) и калькулятор монтажа/витратних,
  - формирование данных КП (4 блока) для превью в HTML,
  - запись позиций в сделку CRM (PATCH /agreements/{id}) — только после подтверждения.

Запуск:  python3 kp_proxy.py         (открыть http://localhost:8790)
"""

import json, re, time, base64, sys, urllib.request, urllib.parse, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Картинки шаблона вшиваются в HTML как data-URI (base64) — видны всегда, и в PDF.
_assets_cache = None
def assets_data_uris():
    global _assets_cache
    if _assets_cache is None:
        HERE_ = HERE
        # Шапка більше не картинка цілком (21.10.2026): контакти в ній були вмальовані в банер,
        # і зміна телефону означала перемальовування файлу. Текст тепер HTML (константа COMPANY
        # у kp_generator.html), а хвиля й логотип вирізаються з того самого banner_hq.jpg
        # засобами CSS (.kp-wave / .kp-logo) — нових файлів у assets/ для цього не треба.
        files = {"banner":   "banner_hq.jpg",     # шапка з оригінального PDF: джерело хвилі й лого
                 "projects": "projects_hq.jpg"}   # реалізовані проєкти (HQ); плитки переваг — HTML
        out = {}
        for k, fn in files.items():
            fp = os.path.join(HERE_, "assets", fn)
            b = base64.b64encode(open(fp, "rb").read()).decode()
            out[k] = "data:image/jpeg;base64," + b
        _assets_cache = out
    return _assets_cache

def _kp_number_crm(commit):
    """Глобальний номер КП, збережений у CRM (ціна службового товару-лічильника). Персистентно."""
    try:
        p = _api(f"/products/sku/{KP_COUNTER_SKU}")
    except urllib.error.HTTPError as e:
        if e.code == 404: p = None
        else: raise
    if not p:   # лічильника ще немає — створюємо
        _api("/products", method="POST", body={
            "sku": KP_COUNTER_SKU, "category_id": KP_COUNTER_CAT, "price": KP_COUNTER_START,
            "title": "Службовий лічильник номерів КП (не чіпати)"})
        p = _api(f"/products/sku/{KP_COUNTER_SKU}")
    n = int(round(float(p.get("price_amount") or p.get("price") or KP_COUNTER_START)))
    if commit:
        _api(f"/products/{p['id']}", method="PATCH", body={"price": n + 1})
    return n

def kp_number(commit=False):
    """Наступний глобальний номер КП. commit=True — зафіксувати (інкремент, при завантаженні PDF).
    Пріоритет: Apps Script (якщо задано) → CRM (у хмарі) → локальний файл (фолбек)."""
    if KP_COUNTER_URL:
        try:
            with urllib.request.urlopen(f"{KP_COUNTER_URL}?commit={'1' if commit else '0'}", timeout=15) as r:
                return int(r.read().decode().strip())
        except Exception: pass
    if KP_COUNTER_CRM:
        try: return _kp_number_crm(commit)
        except Exception: pass
    try: n = int(open(KP_COUNTER_FILE).read().strip())
    except Exception: n = KP_COUNTER_START
    if commit:
        try: open(KP_COUNTER_FILE, "w").write(str(n + 1))
        except Exception: pass
    return n

# ─────────────────────────── КОНФИГ ───────────────────────────
API_BASE   = "https://api.keepincrm.com/v1"
# Токен CRM у коді НЕ зберігається: у хмарі — змінна оточення CRM_TOKEN (Render),
# локально — файл `.crm_token` поруч зі скриптом (у GitHub його не заливаємо).
# Завдяки цьому цей файл можна заливати в репозиторій як є, без «чистої копії».
def _read_token():
    t = os.environ.get("CRM_TOKEN", "").strip()
    if t: return t
    try:
        return open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".crm_token")).read().strip()
    except OSError:
        return ""
API_TOKEN  = _read_token()
# Курс долара живе в САМІЙ CRM — службовий товар зі sku SERVICE-USD-RATE (його ціна = курс).
# API KeepinCRM не віддає ні курс, ні ціни в гривні, тому це єдине місце, де курс задається:
# змінили ціну цього товару в CRM → генератор рахує по-новому (без правки коду й редеплою).
USD_RATE_SKU      = "SERVICE-USD-RATE"
USD_RATE_FALLBACK = float(os.environ.get("USD_RATE", 45.25))   # якщо товар недоступний
EUR_RATE   = None          # евро-оборудование не поддерживаем (в наличии нет)
PORT       = int(os.environ.get("PORT", 8790))   # локально 8790; у хмарі порт задає хостинг
# HERE — папка с kp_generator.html и assets/. В упакованном .exe/.app (PyInstaller)
# файлы распаковываются в sys._MEIPASS, иначе — рядом со скриптом.
HERE       = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

# Безкоштовний Render присипляє сервіс після 15 хв без запитів, і прокидається він
# не 30 с, а до 8 хв (заміряно 03.09.2026: 4 запити поспіль висіли 45-100 с без відповіді,
# менеджер увесь цей час бачить сторінку помилки Render). Тому сервіс сам себе пінгує.
# Лише в робочі години й не в неділю: безкоштовний план дає 750 годин роботи на місяць
# на весь акаунт, цілодобовий пінг з'їв би їх усі й приспав би заразом і crm-proxy.
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL", "")   # Render підставляє сам

def _keep_awake():
    while True:
        t = time.gmtime(time.time() + 3 * 3600)        # Київ = UTC+3
        if 6 <= t.tm_hour < 20 and t.tm_wday != 6:
            try:
                urllib.request.urlopen(SELF_URL + "/api/ping", timeout=20).read()
            except Exception:
                pass
        time.sleep(600)                                # < 15 хв простою

# Глобальний лічильник номерів КП (один на всіх менеджерів).
# У хмарі — Apps Script web app (атомарний, персистентний), URL у змінній KP_COUNTER_URL.
# Локально (без URL) — файл у /tmp як фолбек для тестів.
KP_COUNTER_URL   = os.environ.get("KP_COUNTER_URL", "")   # (опційно) Apps Script web app
KP_COUNTER_CRM   = os.environ.get("KP_COUNTER_CRM", "") == "1"   # у хмарі: зберігати номер у CRM
KP_COUNTER_SKU   = "SERVICE-KP-NUMBER"    # артикул службового товару-лічильника
KP_COUNTER_CAT   = 19                     # категорія «Послуги» (не показується у виборі обладнання)
KP_COUNTER_START = 4600
KP_COUNTER_FILE  = os.path.join("/tmp", "kp_number.txt")   # локальний фолбек

# Отдел продаж: имя в CRM → резервный номер линии, который видит клиент.
# Список сверен с `GET /users` (13.10.2026): все, кроме владельца (id 26), админов
# (Андрій Білий id 1, Green Partners id 4, Асистент id 22) и Олександра Вигулярного (id 24).
# Номера здесь — только резерв: в КП печатается живой номер из телефонии (`phones()`), иначе
# переезд линии между сотрудниками молча отправил бы клиента к другому человеку.
# Замер и проверка источника — `Джерела даних та ціни.md`, п. 7.
MANAGERS = [
    {"name": "Сергій Тангелов",     "phone": "+380937030091"},   # CRM id 27
    {"name": "Олексій Оскольський", "phone": "+380737070049"},   # CRM id 21
    {"name": "Сергій Годун",        "phone": "+380737070046"},   # CRM id 16
    {"name": "Сергій Шабалов",      "phone": "+380737070047"},   # CRM id 25, sales6@green-partners.com.ua
    # Квалификаторы, работают с 05.10.2026 (см. Отдел продаж/CLAUDE.md)
    {"name": "Анастасія Москва",    "phone": "+380731771002"},   # CRM id 28
    {"name": "Антон Гангалюк",      "phone": "+380732477001"},   # CRM id 29
    {"name": "Людмила Волкова",     "phone": "+380631771004"},   # CRM id 30
    {"name": "Денис Тишевич",       "phone": "+380737070041"},   # CRM id 31
    {"name": "Дмитро Хубецов",      "phone": "+380631771015"},   # CRM id 32, перші дзвінки 16.10.2026
]
PHONES_TTL = 600            # кеш номеров из телефонии, с
_phones_cache = {"t": 0, "v": None, "src": "fallback"}

# Типовые станции: инвертор (кВт+фаза) + кол-во АКБ. Панели = под мощность инвертора,
# ёмкость АКБ = стандартная 5 кВт·год (можно поменять в ручном режиме).
STATIONS = [
    {"name": "6 кВт · 1 фаза · 1 АКБ",  "inverter_kw": 6,  "phase": "1ф", "battery_count": 1},
    {"name": "8 кВт · 1 фаза · 1 АКБ",  "inverter_kw": 8,  "phase": "1ф", "battery_count": 1},
    {"name": "10 кВт · 1 фаза · 1 АКБ", "inverter_kw": 10, "phase": "1ф", "battery_count": 1},
    {"name": "10 кВт · 1 фаза · 2 АКБ", "inverter_kw": 10, "phase": "1ф", "battery_count": 2},
    {"name": "10 кВт · 3 фази · 1 АКБ", "inverter_kw": 10, "phase": "3ф", "battery_count": 1},
    {"name": "10 кВт · 3 фази · 2 АКБ", "inverter_kw": 10, "phase": "3ф", "battery_count": 2},
]
DEFAULT_BATTERY_KWH = 5.0   # ёмкость АКБ по умолчанию (для готовых станций)

# Тарифы для блока «Вигоди» (редактируемые дефолты)
PRICE_NOW      = 4.3        # ціна електроенергії зараз, грн/кВт
PRICE_FORECAST = 7.2        # ціна прогнозована (з січня 2027), грн/кВт
GEN_FACTOR     = 1392.636   # генерація кВт·год з 1 кВт/рік — Київська (средняя по Украине)
# Помесячное распределение годовой генерации (доля), профиль Украины; сумма = 1.0.
# Кварталы совпадают с калькулятором економії: Q1=0.17, Q2=0.34, Q3=0.35, Q4=0.14 (зима↓, літо↑).
MONTHS_UA     = ["Січ","Лют","Бер","Кві","Тра","Чер","Лип","Сер","Вер","Жов","Лис","Гру"]
MONTHLY_SHARE = [0.030,0.050,0.090, 0.105,0.120,0.115, 0.125,0.115,0.110, 0.070,0.040,0.030]

# Кредит — ОЩАДБАНК, програма «Енергонезалежність домогосподарств» (КМУ №673, пакет GB — гібридна СЕС)
OSCHAD_COMPENSATION = 0.25             # компенсація тіла кредиту державою
OSCHAD_YEAR_RATES   = [0.0, 0.05, 0.07]  # ставка для клієнта: рік 1 / 2 / 3
UIRD_3M             = 0.21              # роки 4+: ставка = UIRD 3M + 7%
OSCHAD_TERMS_MONTHS = [12, 36, 60]     # рахуємо 1 / 3 / 5 років

# Кредит — УКРСИББАНК (ЄБРР): перший внесок 15%, грейс безпроцентний, після грейсу — щомісячна комісія
# Компенсація банку діє знову (програма активована 20.10.2026) — платежі рахуються від тіла
# МІНУС компенсація. Схема цілком — «Джерела даних та ціни.md», п. 6.
UKRSIB_FIRST        = 0.15             # перший внесок від вартості станції
UKRSIB_COMPENSATION = 0.15             # компенсація банку від тіла кредиту, гасить тіло одразу
UKRSIB_MONTHLY_COMM = 0.035            # щомісячна комісія 3.5% на залишок ПІСЛЯ грейсу
UKRSIB_TERMS        = [(9, 9), (12, 12), (15, 12), (24, 12)]  # (термін_міс, грейс_міс)

# Захисна автоматика — позиция «до 15 кВт» = 281$ (согласовано)
PROTECTION_USD = 281.0
# ID товаров в каталоге CRM для привязки позиций к блоку «Товари» сделки
PROTECTION_ID = 8201228   # Захисна автоматика 281$
VYTRATNI_ID   = 8201225   # Витратні матеріали (кабель, гофра, короб, стяжки, кріпильні)
MONTAZH_ID    = 8201217   # Монтажні та пусконалагоджувальні послуги
# Оборудование берём ТОЛЬКО из папки «Товари в наявності» (id 53) — Deye, низьковольтне,
# гибридные инверторы + LV-АКБ + панели. Другие категории (Solis/FOX/високовольтні) НЕ используем.
IN_STOCK_CATEGORY_ID = 53

# Калькулятор монтажа (грн). Ключи покрытия крыши совпадают с опросником.
# mount — як тип монтажу називається в позиції КП «Монтажні послуги (…)», за калькулятором.
ROOF_OPTIONS = [
    {"key": "metal",       "label": "Металочерепиця / мʼяка покрівля", "mount": "на похилий дах"},
    {"key": "slate",       "label": "Шифер",                          "mount": "на похилий дах"},
    {"key": "natural",     "label": "Натуральна черепиця",             "mount": "на похилий дах"},
    {"key": "flat",        "label": "Плаский дах",                     "mount": "на плаский дах"},
    {"key": "ground_geo",  "label": "Наземна станція (геошуруп)",      "mount": "наземна станція"},
    {"key": "ground_beton","label": "Наземна станція (бетонування)",   "mount": "наземна станція"},
]
# Ставки живуть у Google-таблиці «Калькулятор. Монтаж та Витратні» — її веде власник, генератор тягне
# звідти (RATES_SHEET_ID). Значення нижче — резерв на випадок, коли таблиця недоступна: станом на 21.09.2026.
RATES_SHEET_ID = "1TDJBYvR4-QoXbXRgLDqw_6IOtFmXJOMAIbiF-c4dtJc"
RATES_TTL      = 600        # кеш ставок, с
# Шифер (16.10.2026, рішення власника): 4 000 ₴/панель. Рядка «шифер» у таблиці ще немає, тому ціна
# живе тут не тільки як резерв — вона доїжджає в КП і при живій таблиці. Додасть власник рядок — він перебиває.
FALLBACK_RATES = {
    "panel_install": {"metal": 2875, "natural": 5750, "flat": 3450, "ground_geo": 4600, "ground_beton": 5175,
                      "slate": 4000},
    "panel_consum":  {"metal": 1670, "natural": 3500, "flat": 3000, "ground_geo": 4100, "ground_beton": 3400,
                      "slate": 1670},
    "inverter_install": 12650, "battery_install": 1955, "floor_extra": 4025,
    "inverter_consum": 1500, "battery_consum": 1500, "vyshka": 6000,
    "delivery_per_km": 3.45, "brigade_per_km": 9.2, "per_diem": 1380,
}
TRIP_DAYS = 3               # днів відрядження (фиксировано)

# Ярлык в таблице → ключ ставки. Ищем по куску названия внутри своего блока, а не по номеру строки:
# владелец дописывает строки, и жёсткие индексы разъезжаются молча.
_SHEET_MAP = {
    "монтажні роботи": [("натуральна черепиц", ("panel_install", "natural")), ("плаский дах", ("panel_install", "flat")),
                        ("геошуруп", ("panel_install", "ground_geo")), ("бетонуванн", ("panel_install", "ground_beton")),
                        ("інвертор", ("inverter_install",)), ("акб", ("battery_install",)),
                        ("висотність", ("floor_extra",)), ("металочерепиц", ("panel_install", "metal")),
                        ("шифер", ("panel_install", "slate"))],
    "витратні матеріали": [("натуральна черепиц", ("panel_consum", "natural")), ("баластн", ("panel_consum", "flat")),
                           ("геошуруп", ("panel_consum", "ground_geo")), ("бетонуванн", ("panel_consum", "ground_beton")),
                           ("інвертор", ("inverter_consum",)), ("акб", ("battery_consum",)),
                           ("вишка", ("vyshka",)), ("металочерепиц", ("panel_consum", "metal")),
                           ("шифер", ("panel_consum", "slate"))],
    "відрядження": [("доставка", ("delivery_per_km",)), ("дорога", ("brigade_per_km",)), ("добові", ("per_diem",))],
}
_rates_cache = {"t": 0, "v": None, "src": "fallback"}

def _sheet_rows():
    """Строки таблицы как (назва, ціна). Берём gviz, а не csv: csv отдаёт «3» вместо 3,45 —
    формат ячейки скрывает копейки, и доставка с дорогой приезжают округлёнными."""
    url = f"https://docs.google.com/spreadsheets/d/{RATES_SHEET_ID}/gviz/tq?tqx=out:json&headers=0"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        txt = r.read().decode("utf-8")
    data = json.loads(re.search(r"setResponse\((.*)\);?\s*$", txt, re.S).group(1))
    out = []
    for row in data["table"]["rows"]:
        c = row.get("c") or []
        name = (c[0] or {}).get("v") if len(c) > 0 and c[0] else None
        price = (c[1] or {}).get("v") if len(c) > 1 and c[1] else None
        out.append((re.sub(r"\s+", " ", str(name or "")).strip().lower(), price))
    return out

def _map_rates(rows):
    """(назва, ціна) з таблиці → структура ставок. Кожен ключ має заповнитись рівно один раз:
    якщо два рядки лягли в один ключ, значить ярлики розійшлися і мовчки взялася б чужа ціна."""
    val, block, seen = json.loads(json.dumps(FALLBACK_RATES)), None, []
    for name, price in rows:
        hit = next((b for b in _SHEET_MAP if name.startswith(b)), None)
        if hit: block = hit; continue
        if block is None or price is None: continue
        for needle, path in _SHEET_MAP[block]:
            if needle in name:
                if path in seen: raise ValueError(f"ставка {path} двічі: «{name}»")
                seen.append(path)
                if len(path) == 2: val[path[0]][path[1]] = price
                else: val[path[0]] = price
                break
    # Витратних на шифер у таблиці немає: кріплення те саме, що на похилому даху, тому беремо його
    # ціну, а не резервну — інакше підняття ставки в таблиці шифер молча обійшло б.
    if ("panel_consum", "slate") not in seen:
        val["panel_consum"]["slate"] = val["panel_consum"]["metal"]
    if len(seen) < 15: raise ValueError(f"у таблиці знайдено лише {len(seen)} ставок із 21")
    return val

def _map_phones(items):
    """Имя сотрудника → `did_number` его самого свежего звонка. Именно этот номер набирает
    клиент, когда перезванивает, и он же совпал со всеми четырьмя номерами менеджеров,
    забитыми в коде (замер 13.10.2026: Годун 380737070046, Шабалов …047, Оскольський …049,
    Тангелов 380937030091). Берём только свежайшую запись на человека: за 34 дня три линии
    сменили владельца, и выбор «по большинству» отдал бы Волковій номер Москви."""
    best = {}
    for c in items:
        name = ((c.get("user") or {}).get("name") or "").strip()
        did, at = c.get("did_number"), c.get("started_at") or ""
        if not name or not did:
            continue
        # внутренние линии (8xxxxxxxxxx) клиенту не показываем — это не городской номер
        if not str(did).startswith("380"):
            continue
        if name not in best or at > best[name][1]:
            best[name] = ("+" + str(did), at)
    if not best:
        raise ValueError("в последних звонках нет ни одного did_number")
    return {k: v[0] for k, v in best.items()}

def phones():
    """Номера линий из телефонии CRM; кеш PHONES_TTL, при сбое — номера из MANAGERS.
    Одной страницы хватает: замер 13.10.2026 — в 100 последних звонках есть все 8 человек,
    которые реально звонят (у Хубецова звонков нет вовсе, он и остаётся без номера)."""
    if _phones_cache["v"] and time.time() - _phones_cache["t"] < PHONES_TTL:
        return _phones_cache["v"]
    try:
        val, src = _map_phones(_api("/calls?number=100").get("items") or []), "cdr"
    except Exception as e:
        val, src = {}, "fallback"
        print("phones: беремо резерв,", e, flush=True)
    _phones_cache.update(t=time.time(), v=val, src=src)
    return val

def managers_cfg():
    live = phones()
    return [{"name": m["name"], "phone": live.get(m["name"]) or m["phone"]} for m in MANAGERS]

def rates():
    """Ставки монтажу з Google-таблиці власника; кеш RATES_TTL, при збої — FALLBACK_RATES."""
    if _rates_cache["v"] and time.time() - _rates_cache["t"] < RATES_TTL:
        return _rates_cache["v"]
    try:
        val, src = _map_rates(_sheet_rows()), "sheet"
    except Exception as e:
        val, src = json.loads(json.dumps(FALLBACK_RATES)), "fallback"
        print("rates: беремо резерв,", e, flush=True)
    _rates_cache.update(t=time.time(), v=val, src=src)
    return val

# ─────────────────────── КАТАЛОГ CRM ───────────────────────
_cache = {"t": 0, "products": None}

# Cloudflare у KeepinCRM блокує дефолтний User-Agent «Python-urllib» (403, error 1010),
# тому представляємось звичайним браузером — інакше весь сервіс лягає.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def _api(path, method="GET", body=None):
    url = API_BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-Auth-Token": API_TOKEN,
                                          "Content-Type": "application/json",
                                          "User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 429 у KeepinCRM ловиться живцем навіть на читанні каталогу. Повторюємо ТІЛЬКИ його:
            # повтор PATCH'а, який уже частково застосувався (5xx), наробив би дублів позицій.
            if e.code != 429 or attempt == 2:
                raise
            time.sleep(2 ** attempt)

def fetch_products(force=False):
    """Все товары каталога (кэш 60 c)."""
    if not force and _cache["products"] and time.time() - _cache["t"] < 60:
        return _cache["products"]
    items = []
    page = 1
    while True:
        d = _api(f"/products?page={page}")
        items += d.get("items", [])
        pg = d.get("pagination", {})
        if page >= pg.get("total_pages", 1):
            break
        page += 1
    _cache["products"] = items
    _cache["t"] = time.time()
    return items

def _num(v):
    try: return float(v)
    except (TypeError, ValueError): return None

_rate_cache = {"t": 0, "v": None}

def usd_rate():
    """Курс з CRM (ціна службового товару SERVICE-USD-RATE), кеш 60 с. Немає товару — створюємо."""
    if _rate_cache["v"] and time.time() - _rate_cache["t"] < 60:
        return _rate_cache["v"]
    rate = USD_RATE_FALLBACK
    try:
        try:
            p = _api(f"/products/sku/{USD_RATE_SKU}")
        except urllib.error.HTTPError as e:
            if e.code != 404: raise
            p = None
        if not p:
            _api("/products", method="POST", body={
                "sku": USD_RATE_SKU, "category_id": KP_COUNTER_CAT, "price": USD_RATE_FALLBACK,
                "title": "Службовий курс долара для генератора КП (ціна = курс)"})
            p = _api(f"/products/sku/{USD_RATE_SKU}")
        v = _num(p.get("price_amount"))
        if v and v > 1: rate = v
    except Exception:
        pass
    _rate_cache.update(t=time.time(), v=rate)
    return rate

def price_uah(p):
    """Цена товара в грн: USD×курс з CRM, UAH как есть, EUR не поддерживаем (None)."""
    cur = (p.get("currency") or "UAH").upper()
    amt = _num(p.get("price_amount"))
    if amt is None: return None
    if cur == "UAH": return amt
    if cur == "USD": return round(amt * usd_rate(), 2)
    return None  # EUR и прочее — не поддерживаем

def in_stock(p):
    """В наличии = кастомное поле «Наявність» == 'Так'."""
    return (p.get("custom_fields") or {}).get("Наявність") == "Так"

def phase_of(title):
    t = (title or "").lower()
    if "3 фаз" in t or "трифаз" in t or "трьохфаз" in t or "380" in t: return "3ф"
    if "1 фаз" in t or "однофаз" in t or "одна фаза" in t: return "1ф"
    return "?"

def vendor_of(p):
    """Виробник з кастомного поля CRM «Виробник». У частини АКБ марки в назві немає
    (MonaWall-SE5 — сама характеристика, без бренду), і клієнт отримував у КП батарею без марки.
    Поле заведено мультивибором, тому приїжджає і рядком, і списком."""
    v = (p.get("custom_fields") or {}).get("Виробник")
    if isinstance(v, list):
        v = v[0] if v else None
    v = str(v).strip() if v else ""
    return v or None

BATTERY_PREFIX = "Акумуляторна батарея"

def with_vendor(title, vendor):
    """Назва для КП: марка має бути видно клієнту. Якщо її в назві немає — ставимо одразу після
    «Акумуляторна батарея», як у решти позицій каталогу, а не в кінець рядка характеристик."""
    t = title or ""
    if not vendor or vendor.lower() in t.lower():
        return t
    if t.lower().startswith(BATTERY_PREFIX.lower()):
        return f"{BATTERY_PREFIX} {vendor}{t[len(BATTERY_PREFIX):]}"
    return f"{t} {vendor}"

def power_of(p):
    return _num((p.get("custom_fields") or {}).get("Потужність"))

def classify(p):
    t = (p.get("title") or "").lower()
    if "захисна автоматика" in t: return "protection"
    if "інвертор" in t: return "inverter"
    if any(k in t for k in ["акумулят", "батаре", "акб", "lifepo", "battery"]): return "battery"
    if "панель" in t or "панел" in t: return "panel"
    return "other"

def catalog_grouped():
    """Оборудование в наличии, сгруппированное и с ценой в грн."""
    prods = fetch_products()
    out = {"inverters": [], "batteries": [], "panels": [], "protection": []}
    for p in prods:
        # только папка «Товари в наявності» (id 53) и только реально в наличии
        if (p.get("category") or {}).get("id") != IN_STOCK_CATEGORY_ID or not in_stock(p):
            continue
        uah = price_uah(p)
        if uah is None:  # EUR — пропускаем
            continue
        kind = classify(p)
        rec = {"id": p.get("id"), "sku": p.get("sku"), "title": p.get("title"),
               "uah": uah, "power": power_of(p), "vendor": vendor_of(p),
               "phase": phase_of(p.get("title")), "unit": p.get("unit") or "шт"}
        if kind == "inverter":
            # только гибридные (для приватного дома)
            if "гібрид" in (p.get("title") or "").lower():
                out["inverters"].append(rec)
        elif kind == "battery":
            out["batteries"].append(rec)
        elif kind == "panel":
            out["panels"].append(rec)
    return out

# ─────────────────────── АВТОПОДБОР ───────────────────────
def pick_inverter(cat, kw, phase):
    cands = [x for x in cat["inverters"]
             if x["power"] is not None and abs(x["power"] - kw) < 0.01
             and (phase == "?" or x["phase"] == phase)]
    if not cands:
        return None
    return min(cands, key=lambda x: x["uah"])   # дешевейший в наличии

def pick_battery(cat, kwh):
    cands = [x for x in cat["batteries"] if x["power"] is not None]
    if not cands:
        return None
    # ближайшая ёмкость; при равенстве — дешевле
    best_diff = min(abs(x["power"] - kwh) for x in cands)
    near = [x for x in cands if abs(x["power"] - kwh) - best_diff < 0.2]
    return min(near, key=lambda x: x["uah"])

def choose_battery(cat, battery_id, kwh):
    """Модель АКБ для КП. Менеджер обирає конкретну (у наявності їх кілька з однаковою ємністю —
    Deye, Sunwoda, без бренду), і його вибір сильніший за автопідбір. Якщо вибору немає або товар
    уже зник з наявності — не падаємо, а беремо найближчу ємність (стара поведінка)."""
    if battery_id:
        hit = next((x for x in cat["batteries"] if x["id"] == int(battery_id)), None)
        if hit:
            return hit
    return pick_battery(cat, kwh)

def pick_panel(cat, target_kw):
    """Панель в наличии + количество под целевую мощность (допускается лёгкое превышение)."""
    if not cat["panels"]:
        return None, 0
    panel = min(cat["panels"], key=lambda x: x["uah"])  # в наличии обычно одна (Longi 615)
    pw = (panel["power"] or 0) / 1000.0  # Вт → кВт
    if pw <= 0:
        return panel, 0
    n = max(1, round(target_kw / pw))   # найближча кількість; можливий недобір до пів-панелі
    return panel, n

def choose_panel(cat, panel_id, panel_count, target_kw):
    """Панель для КП: модель і кількість за менеджером, автопідбір — тільки як запас.
    У наявності лежить не одна модель, і автопідбір брав найдешевшу молча; кількість під
    цільову потужність округлялась, тому «14 панелей» менеджера перетворювались у 13."""
    hit = next((x for x in cat["panels"] if x["id"] == int(panel_id)), None) if panel_id else None
    if hit:
        n = int(panel_count or 0)
        if n > 0:
            return hit, n
        pw = (hit["power"] or 0) / 1000.0
        return hit, (max(1, round(target_kw / pw)) if pw > 0 else 0)
    panel, n = pick_panel(cat, target_kw)
    if panel and int(panel_count or 0) > 0:
        n = int(panel_count)
    return panel, n

def panel_options(cat):
    """Панелі в наявності — по рядку на модель (потужність + назва + ціна)."""
    return [{"id": x["id"], "title": x["title"], "wt": int(x["power"] or 0), "uah": x["uah"]}
            for x in sorted(cat["panels"], key=lambda x: (x["power"] or 0, x["uah"]))
            if x["power"]]

def battery_options(cat):
    """Все АКБ в наличии — по одной строке на модель (ёмкость + название + цена).
    Список моделей, а не ёмкостей: при одной и той же ёмкости в наличии лежат разные
    производители, и выбор между ними — за менеджером, не за автоподбором по цене."""
    return [{"id": x["id"], "title": with_vendor(x["title"], x.get("vendor")),
             "kwh": round(x["power"], 2), "uah": x["uah"]}
            for x in sorted(cat["batteries"], key=lambda x: (x["power"] or 0, x["uah"]))
            if x["power"]]

def inverter_options(cat):
    """Уникальные (мощность, фаза) гибридных инверторов в наличии."""
    seen, res = set(), []
    for x in sorted(cat["inverters"], key=lambda i: (i["power"] or 0)):
        key = (x["power"], x["phase"])
        if x["power"] and key not in seen:
            seen.add(key)
            res.append({"kw": x["power"], "phase": x["phase"]})
    return res

# ─────────────────────── КАЛЬКУЛЯТОРЫ ───────────────────────
def round10(x):
    return int(round(x / 10.0) * 10)

def roof_mount(roof):
    """Тип монтажу для назви позиції КП («на похилий дах» / «на плаский дах» / «наземна станція»)."""
    return next((o["mount"] for o in ROOF_OPTIONS if o["key"] == roof), "на похилий дах")

def mounting_rows(n_panels, n_batt, roof, floors, km, override=None, n_inv=1):
    """Детальный монтаж-калькулятор → (rows, totals).
    rows: [{block, key, label, price, qty, sum}] — для прозрачного показа и правки.
    override = {key: qty} — ручные корректировки количеств менеджером.
    Блоки: montazh (Монтажні роботи), vytratni (Витратні матеріали), vidr (Відрядження)."""
    override = override or {}
    R = rates()
    rl = next((o["label"] for o in ROOF_OPTIONS if o["key"] == roof), roof)
    def qty(key, default):
        v = override.get(key)
        return default if v is None else (_num(v) or 0)
    spec = [
        ("montazh",  "m_panel", f"Панелі — {rl}", R["panel_install"].get(roof, 0), n_panels),
        ("montazh",  "m_inv",   "Інвертор",       R["inverter_install"],      n_inv),
        ("montazh",  "m_batt",  "АКБ, BMS",       R["battery_install"],       n_batt),
        ("montazh",  "m_floor", "Висотність (за кожен поверх понад 1) / крутий дах", R["floor_extra"], max(0, floors - 1)),
        ("vytratni", "c_panel", f"Панелі — {rl}", R["panel_consum"].get(roof, 0), n_panels),
        ("vytratni", "c_inv",   "Інвертор",       R["inverter_consum"],       n_inv),
        ("vytratni", "c_batt",  "АКБ",            R["battery_consum"],        n_batt),
        ("vytratni", "c_vyshka","Вишка / маніпулятор (зміна)", R["vyshka"],   0),
        ("vidr",     "d_deliv", "Доставка обладнання (грн/км, туди-назад)",   R["delivery_per_km"], km),
        ("vidr",     "d_road",  "Дорога монтажна бригада (грн/км, туди-назад)", R["brigade_per_km"], km),
        ("vidr",     "d_diem",  "Добові (за день, 3 людини)", R["per_diem"],  TRIP_DAYS),
    ]
    rows = []
    for block, key, label, price, default_qty in spec:
        q = qty(key, default_qty)
        rows.append({"block": block, "key": key, "label": label,
                     "price": price, "qty": q, "sum": round(price * q, 2)})
    D12 = sum(r["sum"] for r in rows if r["block"] == "montazh")
    D23 = sum(r["sum"] for r in rows if r["block"] == "vytratni")
    D29 = sum(r["sum"] for r in rows if r["block"] == "vidr")
    return rows, {"vytratni": round10(D23), "montazh_poslugy": round10(D12 + D29),
                  "D12": D12, "D23": D23, "D29": D29}

def calc_economy(generation_month, consumption):
    """Калькулятор экономии — лист «Власне використання» (Київська область)."""
    g = generation_month
    pay_now  = consumption * PRICE_NOW                       # поточні витрати без СЕС
    pay_raw  = (consumption - g) * PRICE_NOW                 # мінус = перевиробництво
    pay_ses  = max(0.0, pay_raw)                             # у КП рахунок не буває відʼємним
    econ_ses = g * PRICE_NOW                                 # економія із СЕС (= генерація×ціна)
    m1 = g * PRICE_FORECAST                                  # за прогноз тарифом, 1 міс
    return {
        "generation": round(g),
        "consumption": round(consumption),
        "price_now": PRICE_NOW, "price_forecast": PRICE_FORECAST,
        "pay_now": round(pay_now), "pay_ses": round(pay_ses), "econ_ses": round(econ_ses),
        "pay_ses_raw": round(pay_raw),
        "econ_1m": round(m1), "econ_1y": round(m1 * 12),
        "econ_2y": round(m1 * 24), "econ_10y": round(m1 * 120),
    }

def _term_label(months):
    y = months // 12
    return f"{y} рік" if y == 1 else (f"{y} роки" if 2 <= y <= 4 else f"{y} років")

def oschad_credit(price, monthly_econ=0):
    """Кредит Ощадбанк: компенсація тіла 25%, ставка клієнта 0/5/7%/UIRD+7% по годах,
    рівними частинами. Колонки: щомісячний платіж, сума виплати, економія на покупці
    (= Компенсація − Переплата), економія на е/е за строк (= споживання×прогноз×міс)."""
    comp = price * OSCHAD_COMPENSATION
    P = price - comp                       # тіло кредиту, що сплачує клієнт (75%)
    rows = []
    for M in OSCHAD_TERMS_MONTHS:
        interest = 0.0
        for k in range(1, M + 1):
            y = (k - 1) // 12
            rate = OSCHAD_YEAR_RATES[y] if y < 3 else (UIRD_3M + 0.07)
            bal = P - (k - 1) * P / M       # залишок на початок місяця (рівними частинами)
            interest += bal * rate / 12.0
        total = P + interest
        rows.append({"term": _term_label(M), "monthly": round(total / M),
                     "total": round(total), "benefit": round(comp - interest),
                     "elec": round(monthly_econ * M)})
    return {"price": round(price), "compensation": round(comp),
            "principal": round(P), "rows": rows}

def _months_label(m):
    return {9: "9 місяців", 12: "12 місяців", 15: "15 місяців", 24: "2 роки"}.get(m, f"{m} міс")

def ukrsib_credit(price, monthly_econ=0):
    """Кредит Укрсиббанк (ЄБРР). Схема (програма з компенсацією, активована 20.10.2026):
      перший внесок 15% від вартості → тіло = 85% → компенсація банку 15% від тіла гасить
      його одразу → сума до погашення = тіло − компенсація, і платежі рахуються ТІЛЬКИ на неї.
    Погашення рівними частинами; у грейс комісії немає, після грейсу — щомісячна комісія
    3.5% на залишок. Разова комісія банку 1% не враховується (рішення 09.08.2026)."""
    first = price * UKRSIB_FIRST
    body = price - first
    comp = body * UKRSIB_COMPENSATION
    debt = body - comp
    rows = []
    for M, grace in UKRSIB_TERMS:
        comm = 0.0
        for k in range(grace + 1, M + 1):
            bal = debt - (k - 1) * debt / M       # залишок на початок місяця
            comm += bal * UKRSIB_MONTHLY_COMM
        total = debt + comm
        rows.append({"term": _months_label(M), "monthly": round(total / M),
                     "total": round(total), "overpay": round(comm),
                     "elec": round(monthly_econ * M)})
    return {"price": round(price), "first": round(first),
            "first_pct": int(UKRSIB_FIRST * 100), "body": round(body),
            "compensation": round(comp), "comp_pct": int(UKRSIB_COMPENSATION * 100),
            "debt": round(debt), "rows": rows}

def protection_usd(has_panels):
    """Ціна захисної автоматики. Повна — коли в КП і панелі, і обладнання: щит там захищає
    обидві сторони. У ДБЖ (інвертор + АКБ без панелей) ставиться половина щита — половина ціни
    (рішення власника 21.10.2026). Те саме правило діє й на КП із самих панелей, але такого
    формату в опитувальнику немає, і параметра під нього теж — додавати разом із форматом."""
    return PROTECTION_USD if has_panels else PROTECTION_USD / 2

def generation_month(panel_kw):
    return GEN_FACTOR * 0.85 * panel_kw / 12.0

def generation_monthly(panel_kw):
    """Помесячная генерация (кВт·год) — годовая × доля месяца (зима↓, літо↑)."""
    annual = GEN_FACTOR * 0.85 * panel_kw
    return [{"m": MONTHS_UA[i], "kwh": round(annual * MONTHLY_SHARE[i])} for i in range(12)]

# ─────────────────────── СБОРКА КП ───────────────────────
def kp_lines(equip, prot_uah, mtot, mount_label, no_mounting=False, no_protection=False):
    """Позиції КП: обладнання + захисна автоматика + витратні + монтажні послуги.
    У КП «тільки обладнання» монтажні й витратні не додаються зовсім: їхня ціна там = 0,
    і рядок «Монтажні послуги — 0 ₴» клієнт читає як обіцяний безкоштовний монтаж."""
    lines = list(equip)
    if not no_protection:
        lines.append({"title": "Захисна автоматика", "unit": "комплект", "qty": 1,
                      "price": prot_uah, "product_id": PROTECTION_ID})
    if not no_mounting:
        lines += [
            {"title": "Витратні матеріали (кабель, гофра, короб, стяжки, кріпильні матеріали)",
             "unit": "комплект", "qty": 1, "price": mtot["vytratni"], "product_id": VYTRATNI_ID},
            {"title": f"Монтажні послуги ({mount_label}, з доставкою та підключенням)",
             "unit": "послуга", "qty": 1, "price": mtot["montazh_poslugy"], "product_id": MONTAZH_ID},
        ]
    return lines

def build_preview(req):
    """Собирает данные КП по ответам менеджера. req — dict из опросника."""
    # Відстань обовʼязкова: раніше у формі стояв дефолт 600 км, і КП київських обʼєктів
    # їхали з чужими 600 км, а порожнє поле так само тихо обнуляло доставку й дорогу
    # бригади (3,45 + 9,2 ₴/км = 7 620 ₴ на тих же 600 км).
    no_mounting  = bool(req.get("no_mounting"))     # КП тільки на обладнання
    no_protection = bool(req.get("no_protection"))
    # ДБЖ (джерело безперебійного живлення) = інвертор + АКБ (+ захисна автоматика за бажанням).
    # Панелі з такого КП виходять зовсім: інакше вони і в CRM потрапляють рядком при перенесенні
    # товарів, і дають у КП генерацію, якої в ДБЖ немає.
    no_panels    = bool(req.get("no_panels"))
    # Без монтажу доставки й дороги бригади в КП немає, і вимагати відстань нема за що.
    if not no_mounting and not ((_num(req.get("km")) or 0) > 0):
        return {"ok": False, "warnings": ["Вкажіть «Км до обʼєкта (туди-назад)» — "
                "без відстані доставка обладнання і дорога монтажної бригади порахуються в нуль."]}
    cat = catalog_grouped()
    warnings = []

    # 1) параметры оборудования из режима
    if req.get("mode") == "station":
        st = next((s for s in STATIONS if s["name"] == req.get("station")), None)
        if not st:
            return {"error": "Станцію не знайдено"}
        inv_kw, inv_phase = st["inverter_kw"], st["phase"]
        batt_kwh, batt_count = DEFAULT_BATTERY_KWH, st["battery_count"]
        panel_target = inv_kw
        inv_count = 1
    else:  # manual
        inv_kw    = _num(req.get("inverter_kw"))
        inv_phase = req.get("inverter_phase") or "?"
        batt_kwh  = _num(req.get("battery_kwh")) or DEFAULT_BATTERY_KWH
        batt_count = int(req.get("battery_count") or 1)
        panel_target = _num(req.get("panel_kw")) or inv_kw
        inv_count = max(1, int(req.get("inverter_count") or 1))

    # 2) автоподбор в наличии
    inv = pick_inverter(cat, inv_kw, inv_phase)
    if not inv:
        warnings.append(f"Інвертор {inv_kw} кВт {inv_phase} — немає в наявності. Оберіть іншу конфігурацію.")
    batt = choose_battery(cat, req.get("battery_id"), batt_kwh)
    if not batt:
        warnings.append(f"АКБ ~{batt_kwh} кВт·год — немає в наявності.")
    if no_panels:
        panel, n_panels = None, 0
    else:
        panel, n_panels = choose_panel(cat, req.get("panel_id"),
                                       req.get("panel_count"), panel_target)
        if not panel:
            warnings.append("Сонячні панелі — немає в наявності.")

    if warnings and (not inv or not batt or (not no_panels and not panel)):
        return {"warnings": warnings, "ok": False}

    # 3) мощности (Блок 1 «Технічне рішення»)
    panel_kw = round(n_panels * (panel["power"] or 0) / 1000.0, 2) if panel else 0.0
    batt_total = round(batt_count * (batt["power"] or 0), 2)
    # Потужність СЕС = сумарна по інверторах: кілька інверторів ставлять рідко, але ставлять.
    tech = {"ses_kw": round((inv["power"] or 0) * inv_count, 2), "panels_kw": panel_kw,
            "inverter_kw": inv["power"], "inverter_count": inv_count, "battery_kwh": batt_total}

    # 4) список оборудования (Блок 2) с ценами
    prot_uah = round(protection_usd(bool(panel)) * usd_rate(), 2)
    if no_mounting:
        mrows, mtot = [], {"vytratni": 0, "montazh_poslugy": 0, "D12": 0, "D23": 0, "D29": 0}
    else:
        mrows, mtot = mounting_rows(n_panels, batt_count, req.get("roof", "metal"),
                                    int(req.get("floors") or 1), _num(req.get("km")) or 0,
                                    req.get("mounting_override"), n_inv=inv_count)
    equip = [
        {"title": inv["title"],   "unit": "шт",       "qty": inv_count, "price": inv["uah"],   "product_id": inv["id"]},
        # у КП — з маркою; прив'язка позиції до товару CRM іде окремим `product_title` (нижче)
        {"title": with_vendor(batt["title"], batt.get("vendor")),
         "unit": "шт", "qty": batt_count, "price": batt["uah"], "product_id": batt["id"]},
    ]
    if panel:
        equip.append({"title": panel["title"], "unit": "шт", "qty": n_panels,
                      "price": panel["uah"], "product_id": panel["id"]})
    lines = kp_lines(equip, prot_uah, mtot, roof_mount(req.get("roof", "metal")),
                     no_mounting, no_protection)
    # точное название товара из каталога (для привязки позиции к «Товари» без мутации каталога)
    prods_by_id = {p["id"]: p for p in fetch_products()}
    for ln in lines:
        ln["sum"] = round(ln["qty"] * ln["price"], 2)
        p = prods_by_id.get(ln.get("product_id"))
        ln["product_title"] = p["title"] if p else ln["title"]
    total = round(sum(ln["sum"] for ln in lines), 2)

    # 5) блок «Вигоди» + помесячная генерация (для графика)
    econ_month = (_num(req.get("consumption")) or 0) * PRICE_FORECAST
    gen = generation_month(panel_kw)
    econ = calc_economy(gen, _num(req.get("consumption")) or 0)
    monthly = generation_monthly(panel_kw)

    return {
        "ok": True, "warnings": warnings, "no_mounting": no_mounting, "no_panels": no_panels,
        "tech": tech, "lines": lines, "total": total, "economy": econ,
        "monthly": monthly, "mounting": mrows, "mounting_totals": mtot,
        # У КП на ДБЖ кредитних таблиць немає зовсім: обидві рахують економію на е/е, якої
        # без панелей не буває, а держкомпенсація Ощадбанку дається лише на гібридну СЕС.
        "credit": {} if no_panels else {
            "oschad": oschad_credit(total, econ_month),
            "ukrsib": ukrsib_credit(total, econ_month)},
        "meta": {"manager": req.get("manager"), "manager_phone": req.get("manager_phone"),
                 "client_name": req.get("client_name"), "client_phone": req.get("client_phone"),
                 "client_city": req.get("client_city"), "number": req.get("number"),
                 "date": req.get("date")},
    }

# ─────────────────────── СДЕЛКА ───────────────────────
def deal_id_from_url(url):
    # Бойове посилання з CRM — hash-роутер: https://2dce716a.keepincrm.com/#/app/agreements/35566315.
    # Фрагмент відрізати НЕ можна — разом з ним зникає весь ID (менеджер бачив «не вдалося
    # розпізнати ID» на кожному нормальному посиланні). Спершу беремо число прямо після
    # «agreements/», і лише як запасний варіант — останнє довге число поза query: у CRM
    # до посилання чіпляються параметри виду ?stage_id=123456, і останнє число в рядку
    # виявлялось не тим — генератор відкривав чужу угоду.
    u = url or ""
    m = re.search(r"agreements?/(\d{5,})", u)
    if m:
        return m.group(1)
    nums = re.findall(r"(\d{5,})", u.split("?")[0])
    return nums[-1] if nums else None

def _selftest_deal_url():
    cases = [
        ("https://2dce716a.keepincrm.com/#/app/agreements/35566315", "35566315"),
        ("https://2dce716a.keepincrm.com/#/app/agreements/35566315?stage_id=123456", "35566315"),
        ("https://2dce716a.keepincrm.com/#/app/clients/35598801/agreements/35566315", "35566315"),
        ("https://app.keepincrm.com/agreements/35566315", "35566315"),
        ("https://2dce716a.keepincrm.com/#/app/clients/35598801", "35598801"),
        ("", None),
    ]
    for url, want in cases:
        got = deal_id_from_url(url)
        assert got == want, f"{url!r}: {got!r} != {want!r}"
    print("deal_id_from_url — OK")

def _selftest_managers():
    """Імʼя — це value у <select>, по ньому ж шукається телефон (`buildReq`), тому
    дублікат імені молча підставив би в КП чужий номер."""
    names = [m["name"].strip() for m in MANAGERS]
    assert all(names), MANAGERS
    assert len(set(names)) == len(names), sorted(names)
    print("managers — OK (%d осіб)" % len(names))

def _selftest_phones():
    """Сторож вибору номера: свіжіший запис перебиває старіший (лінії переїжджають між людьми),
    внутрішня лінія 800… у КП не потрапляє, а людина без звонків лишається з резервним номером."""
    sample = [
        # старіший запис стоїть першим: порядок видачі CRM не гарантований, вибір — по даті
        {"user": {"name": "Людмила Волкова"}, "did_number": "380731771002", "started_at": "2026-09-20T11:00:00Z"},
        {"user": {"name": "Людмила Волкова"}, "did_number": "380631771004", "started_at": "2026-10-13T11:00:00Z"},
        {"user": {"name": "Анастасія Москва"}, "did_number": "80067124209", "started_at": "2026-10-13T12:00:00Z"},
        {"user": {"name": "Анастасія Москва"}, "did_number": "380731771002", "started_at": "2026-10-13T10:00:00Z"},
        {"user": None, "did_number": "380737070046", "started_at": "2026-10-13T12:30:00Z"},
    ]
    got = _map_phones(sample)
    assert got["Людмила Волкова"] == "+380631771004", got
    assert got["Анастасія Москва"] == "+380731771002", got
    assert len(got) == 2, got
    _phones_cache.update(t=time.time(), v=got, src="test")
    cfg = {m["name"]: m["phone"] for m in managers_cfg()}
    assert cfg["Людмила Волкова"] == "+380631771004", cfg
    assert cfg["Сергій Годун"] == "+380737070046", cfg          # немає у звонках → резерв з MANAGERS
    assert cfg["Дмитро Хубецов"] == "+380631771015", cfg
    _phones_cache.update(t=0, v=None, src="fallback")
    print("phones (номер лінії з телефонії) — OK")

def _selftest_rates():
    """Сторож розбору таблиці: рядок «плаский дах - металочерепиця» не має перебивати похилий дах."""
    sample = [("монтажні роботи", None),
              ("панелі (похилий дах) - металочерепиця, мʼяка покрівля", 2875),
              ("панелі (похилий дах) - натуральна черепиця", 5750),
              ("панелі (плаский дах) - металочерепиця, мʼяка покрівля", 3450),
              ("панелі (наземна станція, під геошуруп)", 4600),
              ("панелі (наземна станція, під бетонування)", 5175),
              ("інвертор", 12650), ("акб, bms", 1955),
              ("додатково, висотність будівлі (за кожен поверх), складність монтажу - крутий дах", 4025),
              ("всього:", None), ("витратні матеріали", None),
              ("панелі (похилий дах) металочерепиця, мʼяка покрівля", 1670),
              ("панелі (похилий дах) натуральна черепиця", 3500),
              ("панелі (плаский дах) баластная система", 3000),
              ("панелі (наземна конструкція, під бетонування, дворядна , двоопорна, холодно-оцинкований профіль)", 3400),
              ("панелі (наземна конструкція, під геошурупи, дворядна , двоопорна, холодно-оцинкований профіль)", 4100),
              ("інвертор", 1500), ("акб", 1500), ("додатково (вишка, маніпулятор) - 1 зміна", 6000),
              ("всього:", None), ("відрядження доставка", None),
              ("доставка обладнання (грн за км)", 3.45), ("дорога монтажна бригада (грн за км)", 9.2),
              ("добові (400 грн/людина)", 1380)]
    r = _map_rates(sample)
    assert r["panel_install"]["metal"] == 2875 and r["panel_install"]["flat"] == 3450, r["panel_install"]
    assert r["panel_consum"]["metal"] == 1670 and r["panel_consum"]["flat"] == 3000, r["panel_consum"]
    assert r["delivery_per_km"] == 3.45 and r["per_diem"] == 1380, r
    # Шифер: своїх рядків у таблиці немає — монтаж лишається 4 000 ₴, витратні дорівнюють похилому даху
    assert r["panel_install"]["slate"] == 4000, r["panel_install"]
    # Витратні на шифер мусять їхати за живою ставкою похилого даху, а не за резервом:
    # піднімаємо її в «таблиці» й перевіряємо, що шифер поїхав разом.
    raised = [(n, 1800 if n.startswith("панелі (похилий дах) металочерепиця") else p) for n, p in sample]
    assert _map_rates(raised)["panel_consum"]["slate"] == 1800, "витратні на шифер відстали від похилого даху"
    r2 = _map_rates(sample[:2] + [("панелі (похилий дах) - шифер", 4300)] + sample[2:])
    assert r2["panel_install"]["slate"] == 4300 and r2["panel_install"]["metal"] == 2875, r2["panel_install"]
    print("rates (розбір таблиці) — OK")

def _selftest_mounting():
    """Сторож кількостей: інвертори й АКБ мають множитись, а не рахуватись по одному."""
    rows, _ = mounting_rows(10, 2, "metal", 1, 600, n_inv=2)
    q = {r["key"]: r["qty"] for r in rows}
    assert q["m_inv"] == 2 and q["c_inv"] == 2, q
    assert q["m_batt"] == 2 and q["c_batt"] == 2, q
    assert q["m_panel"] == 10 and q["c_panel"] == 10, q
    print("mounting_rows (кількості) — OK")

def _selftest_km():
    """Сторож обовʼязкових км: без відстані КП не збирається (доставка порахувалась би в нуль)."""
    for bad in ({}, {"km": ""}, {"km": 0}, {"km": "-5"}):
        r = build_preview(dict(bad, mode="station", station=STATIONS[0]["name"]))
        assert r.get("ok") is False and "Км до обʼєкта" in r["warnings"][0], (bad, r)
    print("build_preview (обовʼязкові км) — OK")

def _selftest_no_mounting():
    """Сторож КП без монтажу: монтажні послуги й витратні не повинні лишатись рядком у нуль."""
    eq = [{"title": "Інвертор", "unit": "шт", "qty": 1, "price": 100.0, "product_id": 1}]
    mt = {"vytratni": 0, "montazh_poslugy": 0}
    full = kp_lines(eq, 50.0, {"vytratni": 7000, "montazh_poslugy": 42000}, "на похилий дах")
    assert len(full) == 4 and full[-1]["price"] == 42000, full
    bare = kp_lines(eq, 50.0, mt, "на похилий дах", no_mounting=True)
    assert [l["product_id"] for l in bare] == [1, PROTECTION_ID], bare
    assert kp_lines(eq, 50.0, mt, "на похилий дах", True, True) == eq, "без автоматики — тільки обладнання"
    # без монтажу відстань не потрібна: КП на обладнання збирається і з порожнім км
    r = build_preview({"mode": "station", "station": STATIONS[0]["name"], "no_mounting": True})
    assert r.get("ok") is not False or "Км до обʼєкта" not in (r.get("warnings") or [""])[0], r
    print("kp_lines (КП без монтажу) — OK")

def _selftest_no_panels():
    """Сторож КП на ДБЖ: панелі не повинні ні потрапити рядком у CRM, ні дати генерацію.
    Перевіряємо і те, що компенсації Ощадбанку тут немає: вона дається на гібридну СЕС."""
    r = build_preview({"mode": "station", "station": STATIONS[0]["name"],
                       "consumption": 500, "no_panels": True, "no_mounting": True})
    assert r.get("ok"), r
    titles = [l["title"].lower() for l in r["lines"]]
    assert not any("панел" in t for t in titles), titles
    assert r["tech"]["panels_kw"] == 0, r["tech"]
    assert r["economy"]["generation"] == 0, r["economy"]
    assert not r["credit"], "без панелей кредитних таблиць у КП немає"
    assert r["economy"]["econ_ses"] == 0, r["economy"]
    # те саме КП з панелями — панелі на місці, інакше сторож ловив би не те
    full = build_preview({"mode": "station", "station": STATIONS[0]["name"],
                          "consumption": 500, "no_mounting": True})
    assert any("панел" in l["title"].lower() for l in full["lines"]), full["lines"]
    assert full["credit"]["oschad"] and full["credit"]["ukrsib"], full["credit"]
    print("build_preview (ДБЖ без панелей) — OK")

def _selftest_battery():
    """Сторож вибору АКБ: у наявності кілька моделей однакової ємності (Deye 5,12 і Sunwoda 5),
    і автопідбір по ціні не має перебивати те, що менеджер обрав руками."""
    cat = {"batteries": [{"id": 1, "title": "без бренду 5", "power": 5.0,  "uah": 48191.0},
                         {"id": 2, "title": "Deye 5,12",    "power": 5.12, "uah": 49322.0},
                         {"id": 3, "title": "Sunwoda 16",   "power": 16.0, "uah": 117650.0}]}
    assert choose_battery(cat, 2, 5.0)["id"] == 2, "вибір менеджера має бути сильнішим за ціну"
    assert choose_battery(cat, None, 5.0)["id"] == 1, "без вибору — найдешевша з найближчою ємністю"
    assert choose_battery(cat, 999, 16.0)["id"] == 3, "зниклий товар — автопідбір, а не падіння"
    assert [o["id"] for o in battery_options(cat)] == [1, 2, 3], "усі моделі, за зростанням ємності"
    print("choose_battery (вибір моделі АКБ) — OK")

def _selftest_vendor():
    """Сторож марки: у КП не має йти АКБ без виробника, і марка не має дублюватись у назві."""
    assert vendor_of({"custom_fields": {"Виробник": ["Sunwoda"]}}) == "Sunwoda", "мультивибір — список"
    assert vendor_of({"custom_fields": {"Виробник": "Deye"}}) == "Deye"
    assert vendor_of({"custom_fields": {}}) is None
    assert with_vendor("Акумуляторна батарея 5 кВт, IP65", "Sunwoda") == \
        "Акумуляторна батарея Sunwoda 5 кВт, IP65", "марку ставимо після «Акумуляторна батарея»"
    assert with_vendor("Акумуляторна батарея Deye LiFePO4", "Deye") == \
        "Акумуляторна батарея Deye LiFePO4", "марка вже в назві — не дублюємо"
    assert with_vendor("Акумуляторна батарея DEYE SE-F16-C", "Deye") == \
        "Акумуляторна батарея DEYE SE-F16-C", "інший регістр — теж не дублюємо"
    assert with_vendor("Акумуляторна батарея 5 кВт", None) == "Акумуляторна батарея 5 кВт"
    print("vendor_of / with_vendor (марка АКБ) — OK")

def _selftest_push_match():
    """Сторож переносу в CRM: ціни розкладаються по id товару, а не по порядку — CRM віддає
    створені позиції не в тому порядку, в якому їх прислали (живцем — угоди 36152950, 36092336,
    36059859: інвертор → панель → АКБ). Плюс нуль у ціні має ловитись, а не їхати як «ок»."""
    lines = [{"title": "Інвертор", "product_id": 11, "price": 60000},
             {"title": "АКБ",      "product_id": 12, "price": 49322.5},
             {"title": "Панель",   "product_id": 13, "price": 6018.25},
             {"title": "Монтажні послуги", "product_id": 14, "price": 80155}]
    jobs = [{"id": 1, "title": "Інвертор",         "product": {"id": 11}},
            {"id": 3, "title": "Панель",           "product": {"id": 13}},
            {"id": 2, "title": "АКБ",              "product": {"id": 12}},
            {"id": 4, "title": "Монтажні послуги", "product": {"id": 14}}]
    pairs = _match_jobs(jobs, lines)
    assert {j["id"]: ln["price"] for j, ln in pairs} == \
        {1: 60000, 2: 49322.5, 3: 6018.25, 4: 80155}, pairs
    assert _match_jobs(jobs[:3], lines) == [], "рядок без пари — писати ціни наосліп не можна"
    deal = {"jobs": [{"id": 1, "price_amount": 60000}, {"id": 2, "price_amount": 49322.5},
                     {"id": 3, "price_amount": 6018.25}, {"id": 4, "price_amount": 0}]}
    assert [ln["title"] for _, ln in _price_gaps(deal, pairs)] == ["Монтажні послуги"]
    print("push_match (порядок CRM ≠ порядок КП) — OK")

def _selftest_economy():
    """Сторож обрізки: при перевиробництві «платити із СЕС» у КП = 0, а не відʼємна сума."""
    e = calc_economy(1500, 300)          # генерація 1500 кВт·год, споживання 300
    assert e["pay_ses"] == 0, e
    assert e["pay_ses_raw"] == round((300 - 1500) * PRICE_NOW), e
    print("calc_economy (перевиробництво) — OK")

def _selftest_ukrsib():
    """Сторож компенсації: платежі мають рахуватись від тіла МІНУС компенсація 15%.
    Якщо базу компенсації змінити (напр. взяти 15% від вартості, а не від тіла) або
    порахувати платежі на повне тіло — числа нижче розійдуться."""
    r = ukrsib_credit(400000)
    assert r["first"] == 60000 and r["body"] == 340000, r
    assert r["compensation"] == 51000 and r["debt"] == 289000, r
    # безпроцентні строки (грейс = строк): виплата = рівно сума до погашення
    assert r["rows"][0]["total"] == 289000 and r["rows"][1]["total"] == 289000, r["rows"][:2]
    assert r["rows"][0]["monthly"] == 32111, r["rows"][0]
    # 2 роки, грейс 12: комісія 3.5% на залишок за місяці 13..24
    assert r["rows"][3]["overpay"] == 32874, r["rows"][3]
    print("ukrsib_credit (з компенсацією 15% тіла) — OK")

def _selftest_protection():
    """Сторож половинного щита: у ДБЖ (без панелей) автоматика коштує рівно половину."""
    assert protection_usd(True) == PROTECTION_USD, protection_usd(True)
    assert protection_usd(False) == PROTECTION_USD / 2, protection_usd(False)
    # Правило має доїхати до рядка КП, а не лишитись у функції: раніше тут підставлялась
    # константа повз protection_usd(), і сторож на самій функції цього не бачив.
    def prot_line(req):
        r = build_preview(req)
        assert r.get("ok"), r
        return next(l["price"] for l in r["lines"] if l["title"] == "Захисна автоматика")
    base = {"mode": "station", "station": STATIONS[0]["name"], "consumption": 500, "no_mounting": True}
    full = prot_line(base)
    ups  = prot_line({**base, "no_panels": True})
    assert abs(ups * 2 - full) <= 0.02, (ups, full)   # копійка на окремому округленні кожного рядка
    print(f"protection_usd (повний {full:.2f} ₴ / половина {ups:.2f} ₴) — OK")

def _selftest_panel():
    """Сторож вибору панелей: модель і кількість від менеджера сильніші за автопідбір."""
    cat = {"panels": [{"id": 7, "title": "Longi 585", "power": 585, "uah": 100},
                      {"id": 8, "title": "Longi 615", "power": 615, "uah": 90}]}
    # 20 ≠ автопідбір (8.0 / 0.585 = 14): інакше сторож зелений і коли кількість ігнорується
    assert choose_panel(cat, 7, 20, 8.0) == (cat["panels"][0], 20)
    # кількість не вказана — добираємо під цільову потужність саме обраної моделі
    assert choose_panel(cat, 7, None, 8.0)[1] == 14          # 8.0 / 0.585 = 13.7
    # модель не обрана: автопідбір по ціні, але кількість менеджера все одно в силі
    p, n = choose_panel(cat, None, 10, 8.0)
    assert p["id"] == 8 and n == 10, (p, n)
    print("choose_panel (модель і кількість від менеджера) — OK")

def fetch_deal(url):
    aid = deal_id_from_url(url)
    if not aid:
        return {"error": "Не вдалося розпізнати ID угоди у посиланні"}
    try:
        d = _api(f"/agreements/{aid}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"error": f"Угоди №{aid} немає в CRM — перевірте посилання"}
        raise
    cl = d.get("client") or {}
    phone = (cl.get("phones") or [None])[0]
    mgr = d.get("main_responsible") or {}
    return {"id": d.get("id"), "title": d.get("title"),
            "client_name": d.get("title"), "client_phone": phone,
            "manager_crm": mgr.get("name")}

def _jobs_count(deal_id):
    try:
        return len(_api(f"/agreements/{deal_id}").get("jobs") or [])
    except Exception:
        return -1

def _match_jobs(jobs, lines):
    """Позиція CRM ↔ рядок КП. Спершу по id товару, потім по точній назві; кожну позицію
    використовуємо один раз. Порожній список = хоч один рядок без пари."""
    free = list(jobs)
    pairs = []
    for ln in lines:
        pid = ln.get("product_id")
        j = next((x for x in free if pid and (x.get("product") or {}).get("id") == pid), None)
        if j is None:
            t = (ln.get("product_title") or ln.get("title") or "").strip().lower()
            j = next((x for x in free if (x.get("title") or "").strip().lower() == t), None)
        if j is None:
            return []
        free.remove(j)
        pairs.append((j, ln))
    return pairs

def _price_gaps(deal, pairs):
    """Пари, де ціна в CRM не дорівнює ціні рядка КП (у т.ч. 0 — каталожна ціна «Витратних»
    і «Монтажних послуг» саме 0, свою ціну ці рядки отримують лише на кроці 2)."""
    now = {j["id"]: j for j in (deal.get("jobs") or [])}
    return [(j, ln) for j, ln in pairs
            if abs((now.get(j["id"], {}).get("price_amount") or 0) - float(ln["price"])) > 0.01]

def push_jobs(deal_id, lines, total):
    """Записать позиции в блок «Товари» сделки. ДВУХШАГОВЫЙ процесс (проверено на CRM):
      Шаг 1 — создать позиции, привязанные к товарам каталога (product_attributes:{id,title});
              цену CRM берёт из каталога, а у «Витратних матеріалів» (8201225) и «Монтажних
              послуг» (8201217) она там 0 — свою цену эти строки получают только на шаге 2.
      Шаг 2 — обновить цену каждой позиции по её id ({id, price}).
    title в product_attributes — ТОЧНОЕ название товара из каталога (иначе 422 «Назва не може бути пустим»).
    Сопоставление позиций со строками КП — по id товара (`_match_jobs`), НЕ по порядку: CRM
    отдаёт созданные позиции не в том порядке, в каком их прислали (живьём — угоди 36152950,
    36092336, 36059859: інвертор → панель → АКБ вместо інвертор → АКБ → панель), и сопоставление
    по индексу ставило цену не в ту строку.
    Цены перечитываем и, если легли не все, дописываем повторно; осталось расхождение —
    возвращаем ошибку, а не «ок»: раньше строка с нулём уезжала к менеджеру под видом успеха."""
    try:
        d0 = _api(f"/agreements/{deal_id}")
    except Exception as e:
        return {"ok": False, "error": f"Не вдалося прочитати угоду: {e}"}
    before_ids = {j["id"] for j in (d0.get("jobs") or [])}

    create = []
    for ln in lines:
        pt = ln.get("product_title") or ln.get("title") or ""
        # currency ставимо явно: каталог CRM увесь у доларах (16 із 16 товарів «у наявності»
        # на 14.10.2026), і позиція, створена без цього поля, може успадкувати USD — тоді в
        # угоді лежить каталожна валюта, а ціна з КП (грн) у неї вже не стає.
        create.append({"title": pt, "amount": ln["qty"], "currency": "UAH",
                       "product_attributes": {"id": ln.get("product_id"), "title": pt}})

    # Шаг 1 — создать позиции
    try:
        _api(f"/agreements/{deal_id}", method="PATCH",
             body={"jobs_attributes": create, "products_total_as_total": True})
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"CRM відхилила створення позицій (HTTP {e.code}): "
                + e.read().decode('utf-8', 'ignore')[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    d1 = _api(f"/agreements/{deal_id}")
    new_jobs = [j for j in (d1.get("jobs") or []) if j["id"] not in before_ids]
    if len(new_jobs) < len(create):
        return {"ok": False, "error": f"CRM створила {len(new_jobs)} із {len(create)} позицій."}

    pairs = _match_jobs(new_jobs, lines)
    if not pairs:
        return {"ok": False, "added": len(new_jobs),
                "error": "Позиції створені, але їх не вдалося зіставити з рядками КП — "
                         "ціни не проставлено, перевірте угоду вручну."}

    # Шаг 2 — проставить цены по id
    def _set_prices(ps):
        _api(f"/agreements/{deal_id}", method="PATCH",
             body={"jobs_attributes": [{"id": j["id"], "currency": "UAH",
                                        "price": f'{float(ln["price"]):.2f}'}
                                       for j, ln in ps],
                   "products_total_as_total": True, "total": round(total, 2)})
    try:
        _set_prices(pairs)
    except urllib.error.HTTPError as e:
        return {"ok": False, "added": len(new_jobs),
                "error": f"Позиції створені, але ціни не проставились (HTTP {e.code})."}

    d2 = _api(f"/agreements/{deal_id}")
    gaps = _price_gaps(d2, pairs)
    if gaps:
        try:
            _set_prices(gaps)
        except urllib.error.HTTPError:
            pass
        d2 = _api(f"/agreements/{deal_id}")
        gaps = _price_gaps(d2, pairs)
    if gaps:
        return {"ok": False, "added": len(new_jobs), "unpriced": [ln["title"] for _, ln in gaps],
                "error": "Ціна не проставилась у рядках: "
                         + "; ".join(ln["title"] for _, ln in gaps)
                         + ". Проставте їх в угоді вручну."}
    return {"ok": True, "added": len(new_jobs), "priced": len(pairs),
            "jobs_total": len(d2.get("jobs") or []), "deal_total": d2.get("total")}

# ─────────────────────── HTTP ───────────────────────
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, obj, code=200, ctype="application/json"):
        raw = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self): self._send(b"", 204)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                fp = os.path.join(HERE, "kp_generator.html")
                if os.path.exists(fp):
                    return self._send(open(fp, "rb").read(), ctype="text/html")
                return self._send(b"<h1>kp_generator.html not found</h1>", 404, "text/html")
            if u.path.startswith("/assets/"):
                name = os.path.basename(u.path)
                fp = os.path.join(HERE, "assets", name)
                if os.path.exists(fp):
                    ct = "image/jpeg" if name.lower().endswith((".jpg", ".jpeg")) else \
                         "image/png" if name.lower().endswith(".png") else "application/octet-stream"
                    return self._send(open(fp, "rb").read(), ctype=ct)
                return self._send(b"not found", 404, "text/plain")
            if u.path == "/api/config":
                cat = catalog_grouped()
                return self._send({"managers": managers_cfg(), "stations": STATIONS,
                                   "roofs": ROOF_OPTIONS,
                                   "inverter_options": inverter_options(cat),
                                   "battery_options": battery_options(cat),
                                   "panel_options": panel_options(cat),
                                   "price_now": PRICE_NOW, "price_forecast": PRICE_FORECAST,
                                   "usd_rate": usd_rate(),
                                   "assets": assets_data_uris()})
            if u.path == "/api/ping":
                return self._send({"ok": True})
            if u.path == "/api/catalog":
                return self._send(catalog_grouped())
            if u.path == "/api/kp-number":
                commit = (q.get("commit", ["0"])[0] == "1")
                return self._send({"number": kp_number(commit)})
            if u.path == "/api/deal":
                return self._send(fetch_deal((q.get("url") or [""])[0]))
        except Exception as e:
            return self._send({"error": str(e)}, 500)
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        ln = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(ln) or "{}")
        try:
            if u.path == "/api/preview":
                return self._send(build_preview(body))
            if u.path == "/api/push":
                return self._send(push_jobs(body["deal_id"], body["lines"], body["total"]))
        except Exception as e:
            return self._send({"error": str(e)}, 500)
        self._send({"error": "not found"}, 404)

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest_deal_url()
        _selftest_managers()
        _selftest_phones()
        _selftest_ukrsib()
        _selftest_panel()
        _selftest_protection()
        _selftest_rates()
        _selftest_mounting()
        _selftest_km()
        _selftest_no_mounting()
        _selftest_no_panels()
        _selftest_battery()
        _selftest_vendor()
        _selftest_push_match()
        _selftest_economy()
        sys.exit(0)
    import webbrowser, threading
    url = f"http://localhost:{PORT}"
    print(f"Генератор КП запущено → {url}   (закрийте це вікно, щоб зупинити)")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    if SELF_URL:
        threading.Thread(target=_keep_awake, daemon=True).start()
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    except OSError as e:
        print(f"\nНе вдалося запустити (порт {PORT} зайнятий?): {e}")
        try: input("Натисніть Enter для виходу...")
        except EOFError: pass
