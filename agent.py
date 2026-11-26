import argparse
import csv
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from statistics import median
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from dotenv import load_dotenv

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from product_utils import matches_name

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)
MSK = timezone(timedelta(hours=3))
USER_AGENT = "PharmaPriceMonitor/0.1"
MONEY_FORMAT = '#,##0.00 "₽"'
SOURCES = {"Ozon": {"ozon.ru", "www.ozon.ru"}, "Ютека": {"uteka.ru", "www.uteka.ru"}}
PARTNER = "Здравсити"
PARTNER_LABEL = "Выгрузка Здравсити"
FEED_FIELDS = ("source", "sku", "city", "seller", "url", "price_type", "availability", "observed_at", "price_rub")
TYPES = {"public": "Публичная", "from": "От", "card": "По карте"}
STATES = {
    "in_stock": "Доступен к заказу", "out_of_stock": "Нет в наличии",
    "unknown": "Наличие неизвестно", "error": "Ошибка проверки",
}


class CardParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.active = None
        self.products = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.active = "title"
        if tag == "script" and dict(attrs).get("type") == "application/ld+json":
            self.active = "json"

    def handle_endtag(self, tag):
        if tag in {"script", "title"}:
            self.active = None

    def handle_data(self, data):
        if self.active == "title":
            self.title += data
        if self.active == "json":
            try:
                parsed = json.loads(data)
            except ValueError:
                return
            nodes = parsed if isinstance(parsed, list) else [parsed]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                graph = node.get("@graph")
                for item in [node, *(graph if isinstance(graph, list) else [])]:
                    if isinstance(item, dict) and "Product" in as_list(item.get("@type")):
                        self.products.append(item)


def as_list(value):
    return value if isinstance(value, list) else [value]


def kopecks(value):
    try:
        amount = Decimal(str(value).replace(" ", "").replace(",", "."))
        cents = amount * 100
        if not cents.is_finite() or not 0 < cents <= 100_000_000 or cents != cents.to_integral_value():
            raise ValueError("Некорректная цена")
        return int(cents)
    except InvalidOperation as exc:
        raise ValueError("Некорректная цена") from exc


def fetch_html(url):
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU"})
    with urlopen(request, timeout=15) as response:
        if urlsplit(response.url).hostname != urlsplit(url).hostname:
            raise ValueError("Страница перенаправлена на другой сайт")
        raw = response.read(6_000_001)
    if len(raw) > 6_000_000:
        raise ValueError("Слишком большой ответ")
    return raw.decode("utf-8")


def parse_card(card, spec, city, html):
    parser = CardParser()
    parser.feed(html)
    city_marker = {"Москва": "в москве"}.get(city)
    if not city_marker or city_marker not in parser.title.casefold():
        raise ValueError("Город по заголовку карточки не подтверждён")
    matches = [item for item in parser.products if str(item.get("sku")) == card["id"]]
    if len(matches) != 1:
        raise ValueError("Структурированные данные нужной карточки не найдены")
    item = matches[0]
    name = str(item.get("name", "")).casefold()
    if not matches_name(name, spec):
        raise ValueError("Название, дозировка, форма или размер упаковки не совпали")
    expected = urlsplit(card["url"])
    offers = []
    for offer in as_list(item.get("offers")):
        if not isinstance(offer, dict) or offer.get("@type") != "Offer" or not offer.get("url"):
            continue
        link = urlsplit(urljoin(card["url"], str(offer["url"])))
        if link.hostname == expected.hostname and link.path.rstrip("/") == expected.path.rstrip("/"):
            offers.append(offer)
    if len(offers) != 1:
        raise ValueError("На карточке нет единственного предложения")
    offer = offers[0]
    state = str(offer.get("availability", "")).rsplit("/", 1)[-1]
    availability = {
        "InStock": "in_stock", "OutOfStock": "out_of_stock", "SoldOut": "out_of_stock",
    }.get(state, "unknown")
    price = None
    if offer.get("price") is not None and availability != "out_of_stock":
        if offer.get("priceCurrency") != "RUB":
            raise ValueError("Валюта предложения отличается от RUB")
        price = kopecks(offer["price"])
    if price is None and availability != "out_of_stock":
        raise ValueError("Цена отсутствует")
    return price, availability


def observation(source, sku, city, seller, url, observed_at, kind, price, state, channel, error=""):
    return {
        "source": source, "sku": sku, "city": city, "seller": seller, "url": url,
        "observed_at": observed_at, "kind": kind, "kopecks": price, "state": state,
        "channel": channel, "error": error,
    }


def http_diagnostic(exc):
    details = [f"HTTP {exc.code}"]
    headers = exc.headers or {}
    if exc.code == 403:
        details.append("отказ в доступе при загрузке страницы")
        if headers.get("cf-mitigated", "").lower() == "challenge":
            details.append("сервер запросил проверку клиента")
    for header in ("Server", "X-Request-ID", "CF-Ray", "Retry-After"):
        value = headers.get(header, "")
        if re.fullmatch(r"[a-zA-Z0-9 .,:_()/-]{1,80}", value):
            details.append(f"{header}={value}")
    return " · ".join(details)


def collect(catalog, snapshots=None):
    rows = []
    city = catalog["city"]
    for index, card in enumerate(catalog["cards"]):
        if index:
            time.sleep(1)
        error = ""
        try:
            html = fetch_html(card["url"])
            if snapshots is not None:
                snapshots[card["url"]] = html
            price, state = parse_card(card, catalog["products"][card["sku"]], city, html)
        except (OSError, ValueError) as exc:
            price, state = None, "error"
            if isinstance(exc, HTTPError):
                error = http_diagnostic(exc)
                exc.close()
            elif isinstance(exc, URLError):
                error = "Сетевая ошибка или тайм-аут"
            else:
                error = str(exc)[:250]
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows.append(observation(card["source"], card["sku"], city, card["source"], card["url"],
                                timestamp, "public", price, state, "site", error))
    return rows


def load_feed(path, catalog, day):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return read_feed(stream, catalog, day)


def read_feed(stream, catalog, day):
    rows = []
    identities = set()
    header = stream.readline()
    stream.seek(0)
    reader = csv.DictReader(stream, delimiter=";" if ";" in header else ",")
    fields = reader.fieldnames or []
    if not set(FEED_FIELDS).issubset(fields) or len(fields) != len(set(fields)):
        raise ValueError("Проверьте обязательные столбцы и повторы заголовков CSV по шаблону.")
    partner_cards = {card["sku"]: card["url"] for card in catalog["cards"] if card["source"] == PARTNER}
    for number, row in enumerate(reader, 2):
        try:
            source, sku, city, seller, url, kind, state, timestamp, raw_price = (
                row[key].strip() for key in FEED_FIELDS
            )
            link = urlsplit(url)
            if None in row:
                raise ValueError("Лишние значения в строке CSV")
            if source == PARTNER and (seller != PARTNER or kind != "public" or url != partner_cards.get(sku)):
                raise ValueError("Для Здравсити нужны публичное предложение площадки и URL этого SKU из каталога")
            hosts = {"zdravcity.ru"} if source == PARTNER else SOURCES.get(source, set())
            if (sku not in catalog["products"] or city != catalog["city"]
                    or kind not in TYPES or state not in {"in_stock", "out_of_stock", "unknown"}
                    or not seller or link.scheme != "https" or link.hostname not in hosts
                    or link.netloc != link.hostname or not link.path.strip("/")):
                raise ValueError("Неподдерживаемый источник, SKU, город, продавец, URL или статус")
            when = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if when.tzinfo is None or when.astimezone(MSK).date() != day:
                raise ValueError("Нужна отметка времени с часовым поясом за текущую дату Москвы")
            if when > datetime.now(timezone.utc) + timedelta(minutes=5):
                raise ValueError("Время наблюдения находится в будущем")
            price = kopecks(raw_price) if raw_price else None
            if (state == "in_stock" and price is None) or (state == "out_of_stock" and price is not None):
                raise ValueError("Цена не согласуется с наличием")
            identity = source, sku, city, seller, kind
            if identity in identities:
                raise ValueError("Дубликат источника, SKU, города, продавца и типа цены")
            identities.add(identity)
        except (AttributeError, KeyError, ValueError) as exc:
            raise ValueError(f"CSV, строка {number}. {exc}") from exc
        rows.append(observation(source, sku, city, seller, url, when.astimezone(timezone.utc)
                                .isoformat(timespec="seconds"), kind, price, state,
                                "partner" if source == PARTNER else "feed"))
    return rows


class FeedRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configured_feed(direct, imports, catalog, day):
    address = os.getenv("ZDRAVCITY_FEED_URL", "").strip()
    covered = {row["sku"] for row in imports if row["channel"] == "partner"}
    denied = [row for row in direct if row["source"] == PARTNER and row["error"].startswith("HTTP 403")
              and row["sku"] not in covered]
    if not address or not denied:
        return []
    try:
        link = urlsplit(address)
        if link.scheme != "https" or not link.hostname or link.username is not None or link.password is not None:
            raise ValueError("Нужна HTTPS-ссылка на выгрузку")
        headers = {"Accept": "text/csv", "User-Agent": USER_AGENT}
        token = os.getenv("ZDRAVCITY_FEED_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with build_opener(FeedRedirectHandler()).open(Request(address, headers=headers), timeout=20) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("CSV превышает 2 МБ")
        rows = read_feed(io.StringIO(raw.decode("utf-8-sig"), newline=""), catalog, day)
        if not rows or any(row["channel"] != "partner" for row in rows):
            raise ValueError("Нужны строки согласованной выгрузки Здравсити")
        by_sku = {row["sku"]: row for row in rows}
        for row in denied:
            if row["sku"] not in by_sku:
                row["error"] += " · для SKU нет строки в выгрузке"
        return [by_sku[row["sku"]] for row in denied if row["sku"] in by_sku]
    except HTTPError as exc:
        detail = f"HTTP {exc.code}"
        exc.close()
    except (OSError, HTTPException, ValueError, csv.Error):
        detail = "проверьте доступ, формат CSV и дату наблюдений"
    for row in denied:
        row["error"] += f" · выгрузка недоступна ({detail})"
    return []


def apply_feed(direct, imports):
    partner = {row["sku"]: row for row in imports if row["channel"] == "partner"}
    current = []
    for row in direct:
        fallback = partner.get(row["sku"]) if row["source"] == PARTNER else None
        if fallback and row["state"] == "error":
            current.append(dict(fallback, error=f'{row["error"]} · попытка {row["observed_at"]}'))
        else:
            current.append(row)
    current.extend(row for row in imports if row["channel"] != "partner")
    return current


def save_run(database, run_id, rows):
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                run_id TEXT NOT NULL, source TEXT NOT NULL, sku TEXT NOT NULL, city TEXT NOT NULL,
                seller TEXT NOT NULL, url TEXT NOT NULL, observed_at TEXT NOT NULL, kind TEXT NOT NULL,
                kopecks INTEGER, state TEXT NOT NULL, channel TEXT NOT NULL, error TEXT NOT NULL,
                PRIMARY KEY (run_id, source, sku, city, seller, kind)
            )
        """)
        connection.executemany("""
            INSERT INTO observations VALUES (
                :run_id, :source, :sku, :city, :seller, :url, :observed_at,
                :kind, :kopecks, :state, :channel, :error
            )
        """, [dict(row, run_id=run_id) for row in rows])
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(
            "SELECT * FROM observations WHERE city = ? ORDER BY run_id, source, sku, seller, kind",
            (rows[0]["city"],),
        )]


def comparison_key(row):
    return (row["channel"], row["source"], row["sku"], row["seller"], row["kind"], row["url"])


def summarize(catalog, current, history, day):
    available = [r for r in current if r["state"] == "in_stock" and r["kopecks"] is not None]
    overview = []
    for sku, spec in catalog["products"].items():
        prices = [row["kopecks"] for row in available if row["sku"] == sku
                  and row["channel"] in {"site", "partner"} and row["kind"] == "public"]
        overview.append({
            "brand": spec["brand"], "dose": spec["dose"], "pack": spec["pack"],
            "offers": len(prices), "min_rub": min(prices) / 100 if prices else None,
            "median_rub": median(prices) / 100 if prices else None,
            "max_rub": max(prices) / 100 if prices else None,
        })
    previous = {}
    for row in sorted(history, key=lambda item: item["observed_at"]):
        observed_day = datetime.fromisoformat(row["observed_at"]).astimezone(MSK).date()
        if observed_day == day - timedelta(days=1):
            previous[comparison_key(row)] = row
    changes = []
    for row in available:
        old = previous.get(comparison_key(row))
        if old and old["state"] == "in_stock" and old["kopecks"]:
            diff = (row["kopecks"] - old["kopecks"]) / old["kopecks"] * 100
            changes.append({
                "source": row["source"], "seller": row["seller"],
                "brand": catalog["products"][row["sku"]]["brand"],
                "dose": catalog["products"][row["sku"]]["dose"],
                "pack": catalog["products"][row["sku"]]["pack"], "price_type": TYPES[row["kind"]],
                "previous_rub": old["kopecks"] / 100, "current_rub": row["kopecks"] / 100,
                "delta_pct": round(diff, 2),
            })
    return overview, changes


def openai_note(overview, changes, total, errors):
    model = os.getenv("OPENAI_MODEL", "gpt-6-luna")
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return "Сводка ИИ пропущена. Задайте OPENAI_API_KEY, чтобы включить её."
    if not any(row["offers"] for row in overview):
        return "Сводка ИИ пропущена. В этом запуске отсутствуют проверенные предложения в наличии."
    direct_changes = [change for change in changes if change["source"] not in SOURCES]
    payload = {
        "model": model, "store": False, "reasoning": {"effort": "none"},
        "instructions": (
            "Напиши краткий комментарий на русском для коммерческого отчёта о мониторинге аптек. "
            "Используй только переданные агрегаты. Не копируй числовые значения, они уже есть в таблице. "
            "Не делай выводов о рынке, спросе, лечении или причинах изменений. "
            "Отметь неполное покрытие, если есть ошибки. Максимум три коротких предложения."
        ),
        "input": json.dumps({"overview": overview, "day_over_day": direct_changes,
                             "cards": total, "errors": errors}, ensure_ascii=False),
        "max_output_tokens": 350,
    }
    request = Request("https://api.openai.com/v1/responses",
                      data=json.dumps(payload, ensure_ascii=False).encode(), headers={
                          "Authorization": "Bearer " + key, "Content-Type": "application/json",
                      })
    try:
        with urlopen(request, timeout=35) as response:
            result = json.load(response)
        if result.get("status") != "completed":
            return f"Сводка ИИ недоступна. Ответ модели {model} не завершён."
        parts = [part["text"] for item in result.get("output", []) if item.get("type") == "message"
                 for part in item.get("content", []) if part.get("type") == "output_text"]
        if parts:
            return f"{model}. " + "\n".join(parts).strip()[:1500]
        return f"Сводка ИИ недоступна. Модель {model} вернула пустой ответ."
    except HTTPError as exc:
        return f"Сводка ИИ недоступна. OpenAI API вернул HTTP {exc.code}."
    except (OSError, ValueError):
        return "Сводка ИИ недоступна. Ошибка соединения или формата ответа OpenAI API."


def format_table(sheet, widths, header=1, last_row=None):
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for cell in sheet[header][:len(widths)]:
        cell.fill = PatternFill("solid", fgColor="173B49")
        cell.font = Font(name="Calibri", color="FFFFFF", bold=True)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[header].height = 31
    sheet.freeze_panes = f"A{header + 1}"
    end = last_row or sheet.max_row
    for cells in sheet.iter_rows(min_row=header + 1, max_row=end, max_col=len(widths)):
        for cell in cells:
            if isinstance(cell.value, str):
                cell.data_type = "s"
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            if cell.row % 2 == 0:
                cell.fill = PatternFill("solid", fgColor="EEF5F5")
        sheet.row_dimensions[cells[0].row].height = 31


def add_observations(book, title, rows, products):
    sheet = book.create_sheet(title)
    sheet.append(["Площадка", "Продавец", "Город", "Препарат", "Дозировка", "Форма", "Шт.",
                  "Тип цены", "Цена за уп.", "Цена за шт.", "Наличие", "Время МСК", "Канал",
                  "Карточка", "Диагностика"])
    for row in rows:
        if row["sku"] not in products:
            continue
        spec = products[row["sku"]]
        price = row["kopecks"]
        time_msk = datetime.fromisoformat(row["observed_at"]).astimezone(MSK).replace(tzinfo=None)
        sheet.append([row["source"], row["seller"], row["city"], spec["brand"], spec["dose"],
                      spec["form"], spec["pack"], TYPES[row["kind"]],
                      price / 100 if price is not None else None,
                      price / 100 / spec["pack"] if price is not None else None,
                      STATES[row["state"]], time_msk, {"site": "Сайт", "partner": PARTNER_LABEL}.get(row["channel"], "CSV"),
                      "Открыть", row["error"]])
        index = sheet.max_row
        for column in ("I", "J"):
            sheet[f"{column}{index}"].number_format = MONEY_FORMAT
        sheet[f"L{index}"].number_format = "dd.mm.yyyy hh:mm:ss"
        link = sheet[f"N{index}"]
        link.hyperlink = row["url"]
        link.font = Font(color="1E6074", underline="single")
    format_table(sheet, [16, 20, 14, 16, 14, 31, 8, 15, 15, 15, 25, 23, 10, 14, 49])
    sheet.auto_filter.ref = sheet.dimensions


def write_report(path, catalog, current, history, overview, changes, note, day, research=None, internet=None):
    book = Workbook()
    sheet = book.active
    sheet.title = "Сводка"
    sheet.append([f"Мониторинг цен  /  {catalog['city']}  /  {day:%d.%m.%Y}"])
    sheet.merge_cells("A1:G1")
    sheet["A1"].font = Font(size=16, bold=True, color="173B49")
    sheet.row_dimensions[1].height = 26
    sheet.append(["Препарат", "Дозировка", "Шт. в уп.", "Предложений", "Минимум", "Медиана", "Максимум"])
    for row in overview:
        sheet.append(list(row.values()))
        for column in ("E", "F", "G"):
            sheet[f"{column}{sheet.max_row}"].number_format = MONEY_FORMAT
    format_table(sheet, [22, 16, 12, 17, 17, 17, 17], header=2)
    direct = [r for r in current if r["channel"] == "site"]
    replaced = sum(r["channel"] == "partner" for r in current)
    errors = sum(r["state"] == "error" for r in direct)
    imports = len(current) - len(direct)
    for line in [
        f"Прямые проверки: {len(direct) - errors} из {len(direct) + replaced} успешны. Строк из CSV: {imports}.",
        "В сводке только публичные цены в наличии с сайта или из согласованной выгрузки Здравсити.",
        "«Изменения» сравнивает цены той же карточки, продавца и типа за вчера по Москве.",
        "Ozon и Ютека подключаются через CSV. Условия и типы цен доступны на листе «Проверки».",
        "Комментарий ИИ",
        note,
    ]:
        sheet.append([line])
        index = sheet.max_row
        sheet.merge_cells(start_row=index, start_column=1, end_row=index, end_column=7)
        cell = sheet.cell(index, 1)
        cell.data_type = "s"
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        sheet.row_dimensions[index].height = max(24, 16 * (1 + len(line) // 85))
    add_observations(book, "Проверки", current, catalog["products"])
    visible_history = [row for row in history if row["sku"] in catalog["products"]]
    add_observations(book, "История", visible_history, catalog["products"])
    delta = book.create_sheet("Изменения")
    delta.append(["Площадка", "Продавец", "Препарат", "Дозировка", "Шт. в уп.",
                  "Тип цены", "Цена вчера", "Цена сейчас", "Изменение, %"])
    for change in changes:
        delta.append(list(change.values()))
        for column in ("G", "H"):
            delta[f"{column}{delta.max_row}"].number_format = MONEY_FORMAT
        delta[f"I{delta.max_row}"].number_format = "0.00"
    format_table(delta, [17, 22, 18, 14, 12, 16, 17, 17, 18])
    if not changes:
        delta.append(["Нет сопоставимых проверок за вчера"])
        delta.merge_cells("A2:I2")
    if research is not None:
        from market_research import add_sheets

        add_sheets(book, research, format_table, MONEY_FORMAT)
    if internet is not None:
        from internet_monitor import add_excel_sheets
        add_excel_sheets(book, internet, format_table)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".xlsx", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        book.save(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Мониторинг цен аптечных карточек")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--feed", type=Path, help="CSV с актуальными ценами Здравсити, Ozon или Ютеки")
    parser.add_argument("--no-ai", action="store_true", help="Сформировать отчёт без OpenAI")
    parser.add_argument("--prices-only", action="store_true", help="Собрать прежний отчёт по ценам")
    parser.add_argument("--research-config", type=Path, default=ROOT / "research_config.json")
    parser.add_argument("--visibility-feed", type=Path, help="CSV с фактическими позициями в выдаче")
    parser.add_argument("--search-snapshots", type=Path, help="CSV-манифест сохранённых HTML выдачи")
    parser.add_argument("--skip-internet", action="store_true", help="Отключить интернет-мониторинг")
    parser.add_argument("--internet-config", type=Path, default=ROOT / "internet_config.json")
    parser.add_argument("--output", type=Path, help="Путь нового Excel-отчёта")
    parser.add_argument("--no-internet-search", action="store_true", help="Не вызывать поисковые API и Threads")
    args = parser.parse_args()
    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    now = datetime.now(MSK)
    day = now.date()
    try:
        if args.prices_only and (args.visibility_feed or args.search_snapshots):
            raise ValueError("Источники видимости требуют включённого исследования")
        if not args.prices_only:
            from market_research import prepare_visibility, run_research

            setup = prepare_visibility(
                args.research_config, catalog, day, args.visibility_feed, args.search_snapshots, now
            )
        imports = load_feed(args.feed, catalog, day) if args.feed else []
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    args.data_dir.mkdir(parents=True, exist_ok=True)
    snapshots = {} if not args.prices_only else None
    direct = collect(catalog, snapshots)
    imports.extend(configured_feed(direct, imports, catalog, day))
    if datetime.now(MSK).date() != day:
        parser.error("Во время сбора сменилась дата Москвы. Запустите сбор повторно.")
    current = apply_feed(direct, imports)
    history = save_run(args.data_dir / "prices.sqlite3", now.isoformat(timespec="microseconds"), current)
    overview, changes = summarize(catalog, current, history, day)
    errors = sum(row["state"] == "error" for row in direct)
    note = ("Сводка ИИ отключена для этого запуска." if args.no_ai
            else openai_note(overview, changes, len(direct), errors))
    output = args.output or args.data_dir / f"prices_{now:%Y-%m-%d_%H%M%S_%f}.xlsx"
    output.parent.mkdir(parents=True, exist_ok=True)
    research = None
    if snapshots is not None:
        research = run_research(catalog, snapshots, args.data_dir / "research.sqlite3",
                                day, args.no_ai, setup, fetch_html)
    internet = None
    if not args.skip_internet and not args.prices_only:
        from internet_monitor import collect as collect_internet, load_config, write_snapshot
        try:
            internet = collect_internet(load_config(args.internet_config), args.data_dir / "internet.sqlite3",
                                        no_ai=args.no_ai, use_search=not args.no_internet_search)
            write_snapshot(args.data_dir / "internet_snapshot.json", internet)
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            print(f"Интернет-мониторинг не завершён: {exc}", file=sys.stderr)
    write_report(output, catalog, current, history, overview, changes, note, day, research, internet)
    print(f"Карточки: {len(direct) - errors}/{len(direct)}. Строк из CSV: {len(imports)}. Отчёт: {output}")
    return 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
