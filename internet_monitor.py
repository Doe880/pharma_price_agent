import argparse
import csv
import hashlib
import json
import os
import sqlite3
import uuid
import tempfile
from collections import Counter, defaultdict, deque
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from lxml import etree

from internet_search import brave_results, classify_pages, discovery_queries, openai_sources
from internet_sources import Fetcher, brand_mentions, canonical, duplicate_group, feed_links, index_links, normalize, parse_page

ROOT = Path(__file__).resolve().parent
MSK = timezone(timedelta(hours=3))
TABLES = {
    "Упоминания интернета": ["Бренд", "Роль", "Заголовок", "Тип", "Тема", "Аудитория", "Основное сообщение",
                           "Дата публикации", "Обнаружено", "Проверено", "Домен", "Группа", "Источник", "Разметка"],
    "Вопросы покупателей": ["Бренд", "Вопрос", "Тип источника", "Обнаружено", "Источник"],
    "Материалы о продуктах": ["Бренд", "Заголовок", "Тип", "Тема", "Аудитория", "Основное сообщение", "Дата публикации",
                             "Обнаружено", "Источник", "Разметка"],
    "Реклама в интернете": ["Бренд", "Текст предложения", "Механика", "Маркировка", "Проверено", "Источник"],
    "Ассортимент интернета": ["Бренд", "Название", "Форма", "Масса или дозировка", "Упаковка",
                             "Впервые обнаружено", "Последняя проверка", "Основание", "Источник"],
    "Интернет конкурентов": ["Бренд", "Роль", "Уникальных материалов", "Площадок", "Вопросов",
                             "Промопредложений", "Позиций ассортимента"],
    "Поиск в интернете": ["Бренд", "Запрос", "Провайдер", "Регион", "Дата", "Позиция", "Глубина", "Заголовок",
                         "Домен", "Официальный домен", "Источник"],
    "Источники интернета": ["Дата", "Источник", "Статус", "Комментарий"],
}
STATUSES = {"ok": "Обработан", "error": "Ошибка", "info": "Информация"}
META = (
    "Учтены только страницы, на которых подтверждено упоминание бренда.",
    "Обнаружено — дата первого сбора. Дата публикации заполняется по данным страницы.",
    "Группа объединяет одинаковые и близкие тексты. Метрики упоминаний считаются по уникальным группам.",
    "Позиции Brave Search и импортированной выдачи относятся к указанному поисковому провайдеру.",
    "Порядок найденных источников OpenAI не является позицией в поисковой выдаче.",
    "Новая строка ассортимента означает первое наблюдение источника; дату вывода продукта нужно проверить.",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_database(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS web_documents(
          url TEXT PRIMARY KEY, first_seen TEXT, checked TEXT, group_id TEXT, content_hash TEXT, data TEXT
        );
        CREATE TABLE IF NOT EXISTS web_labels(
          content_hash TEXT, model TEXT, version INTEGER, data TEXT,
          PRIMARY KEY(content_hash,model,version)
        );
        CREATE TABLE IF NOT EXISTS web_diagnostics(
          id INTEGER PRIMARY KEY, checked TEXT, url TEXT, status TEXT, message TEXT
        );
        CREATE TABLE IF NOT EXISTS web_search(
          provider TEXT, query TEXT, region TEXT, checked TEXT, brand TEXT, url TEXT, data TEXT,
          PRIMARY KEY(provider,query,region,checked,brand,url)
        );
        CREATE TABLE IF NOT EXISTS web_products(
          brand TEXT, name TEXT, url TEXT, first_seen TEXT, checked TEXT, data TEXT,
          PRIMARY KEY(brand,name,url)
        );
        CREATE TABLE IF NOT EXISTS web_runs(
          id TEXT PRIMARY KEY, started TEXT, finished TEXT, status TEXT
        );
        CREATE TABLE IF NOT EXISTS web_state(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS web_lock(
          id INTEGER PRIMARY KEY CHECK(id=1), token TEXT, expires REAL
        );
    """)
    return db


@contextmanager
def collection_lock(db):
    token = uuid.uuid4().hex
    now = datetime.now(timezone.utc).timestamp()
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT expires FROM web_lock WHERE id=1").fetchone()
        if row and row["expires"] > now:
            raise ValueError("Сбор интернет-материалов уже выполняется. Дождитесь его завершения.")
        db.execute("INSERT OR REPLACE INTO web_lock VALUES(1,?,?)", (token, now + 3600))
    try:
        yield token
    finally:
        with db:
            db.execute("DELETE FROM web_lock WHERE id=1 AND token=?", (token,))


def load_config(path=ROOT / "internet_config.json", comparison=None):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if comparison is not None:
        config["comparison_brands"] = comparison
    brands = config["brands"] + config.get("comparison_brands", [])
    names = set()
    for brand in brands:
        name = brand["name"]
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("Имена интернет-брендов должны быть непустыми и уникальными")
        if not isinstance(brand["aliases"], list) or not brand["aliases"] or any(
            not isinstance(alias, str) or len(alias.strip()) < 3 for alias in brand["aliases"]
        ):
            raise ValueError("Для каждого бренда нужны поисковые названия длиной от трёх символов")
        if brand.get("role") not in {"Основной", "Для сравнения"}:
            raise ValueError("Неизвестная роль бренда")
        names.add(name)
    for key, maximum in (("max_pages", 100), ("max_discovery_queries", 12), ("request_timeout", 30)):
        if type(config[key]) is not int or not 1 <= config[key] <= maximum:
            raise ValueError(f"Некорректное ограничение {key}")
    if not 0.5 <= float(config["request_delay"]) <= 10:
        raise ValueError("Пауза между запросами должна быть от 0,5 до 10 секунд")
    for source in config["sources"]:
        source["url"] = canonical(source["url"])
        if source["kind"] not in {"page", "index", "feed"} or not set(source["brands"]) <= names:
            raise ValueError("Источник содержит неизвестные бренды или тип")
    config["all_brands"] = brands
    return config


def diagnostic(db, url, status, message):
    with db:
        db.execute("INSERT INTO web_diagnostics(checked,url,status,message) VALUES(?,?,?,?)",
                   (utc_now(), url, status, str(message)[:500]))


def read_imports(path, config):
    if path is None:
        return []
    names = {brand["name"] for brand in config["all_brands"]}
    rows = []
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not {"brand", "url"} <= set(reader.fieldnames or []):
            raise ValueError("В CSV ссылок нужны столбцы brand,url")
        for number, row in enumerate(reader, 2):
            if row.get("brand") not in names or row.get("url") is None:
                raise ValueError(f"Неизвестный бренд или ссылка в строке {number}")
            rows.append({"url": canonical(row["url"]), "brands": [row["brand"]], "kind": "page"})
    if len(rows) > 100:
        raise ValueError("В одном CSV допускается до 100 ссылок")
    return rows


def save_search(db, provider, query, region, checked, brand, rows, depth):
    # Empty result is retained as a completed check, distinguishable from an API error.
    rows = rows or [{"url": "", "title": "", "position": None, "depth": depth}]
    with db:
        db.executemany("INSERT OR REPLACE INTO web_search VALUES(?,?,?,?,?,?,?)", [
            (provider, query, region, checked, brand, row["url"], json.dumps(dict(row, depth=depth), ensure_ascii=False))
            for row in rows
        ])


def import_search(db, path, config):
    if path is None:
        return
    fields = ("brand", "query", "provider", "region", "observed_at", "url", "title", "position", "depth")
    brands = {brand["name"] for brand in config["all_brands"]}
    prepared = defaultdict(list)
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not set(fields) <= set(reader.fieldnames or []):
            raise ValueError("Неверные столбцы CSV поисковой выдачи")
        seen = set()
        for number, row in enumerate(reader, 2):
            try:
                if None in row or any(row.get(field) is None for field in fields):
                    raise ValueError("Пропущены поля")
                row = {field: row[field].strip() for field in fields}
                stamp = datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
                if stamp.tzinfo is None or stamp > datetime.now(timezone.utc) + timedelta(minutes=5):
                    raise ValueError("Нужны фактическая дата и часовой пояс")
                depth = int(row["depth"])
                rank = int(row["position"]) if row["position"] else None
                link = canonical(row["url"]) if row["url"] else ""
                if (row["brand"] not in brands or not row["query"] or not row["provider"] or not row["region"]
                        or not 0 <= depth <= 100 or (link and not 1 <= (rank or 0) <= depth)
                        or (not link and (rank is not None or depth != 0))):
                    raise ValueError("Неверные бренд, позиция, глубина или ссылка")
                key = row["brand"], row["query"], row["provider"], row["region"], stamp.isoformat()
                identity = key, link, rank
                if identity in seen:
                    raise ValueError("Дубликат поискового результата")
                seen.add(identity)
                prepared[key].append({"url": link, "title": row["title"][:350], "position": rank, "depth": depth})
            except (ValueError, TypeError) as exc:
                raise ValueError(f"CSV поиска, строка {number}. {exc}") from None
    for key, rows in prepared.items():
        ranks = [row["position"] for row in rows if row["position"] is not None]
        if len(set(row["depth"] for row in rows)) != 1 or len(set(ranks)) != len(ranks):
            raise ValueError("Глубина выдачи неодинакова или позиции повторяются")
        if any(not row["url"] for row in rows) and len(rows) != 1:
            raise ValueError("Пустую выдачу следует передавать одной строкой")
    with db:
        for (brand, query, provider, region, stamp), rows in prepared.items():
            save_search(db, provider, query, region, stamp, brand, rows, rows[0]["depth"])


def collect(config, database, api_key=None, use_search=True, no_ai=False, url_import=None, search_import=None):
    sources = config["sources"] + read_imports(url_import, config)
    with closing(open_database(database)) as db, collection_lock(db) as token:
        run_id = uuid.uuid4().hex
        with db:
            db.execute("INSERT INTO web_runs VALUES(?,?,NULL,?)", (run_id, utc_now(), "running"))
        try:
            import_search(db, search_import, config)
            _collect(db, config, sources, api_key, use_search, no_ai)
        except BaseException:
            with db:
                db.execute("UPDATE web_runs SET finished=?,status='failed' WHERE id=?", (utc_now(), run_id))
            raise
        with db:
            db.execute("UPDATE web_runs SET finished=?,status='finished' WHERE id=?", (utc_now(), run_id))
        return snapshot(db, config)


def _collect(db, config, sources, api_key, use_search, no_ai):
    brands = config["all_brands"]
    found = list(sources)
    region = f"{config['country']} / {config['city']}"
    key = None if no_ai else (api_key or os.getenv("OPENAI_API_KEY", "")).strip()
    if use_search:
        queries = discovery_queries(brands)
        state = db.execute("SELECT value FROM web_state WHERE key='query_cursor'").fetchone()
        cursor = int(state["value"]) if state else 0
        attempts = min(config["max_discovery_queries"], len(queries))
        if not key and not os.getenv("BRAVE_SEARCH_API_KEY"):
            diagnostic(db, "", "info", "Для поиска новых внешних страниц нужен OPENAI_API_KEY либо BRAVE_SEARCH_API_KEY.")
            attempts = 0
        for index in range(attempts):
            brand, query = queries[(cursor + index) % len(queries)]
            stamp = utc_now()
            try:
                if os.getenv("BRAVE_SEARCH_API_KEY"):
                    rows, depth = brave_results(query, os.environ["BRAVE_SEARCH_API_KEY"], config["country"])
                    provider = "Brave Search"
                else:
                    rows = openai_sources(query, key, config["city"], config["country"])
                    depth, provider = None, "OpenAI web search"
                save_search(db, provider, query, region, stamp, brand, rows, depth)
                found.extend({"url": row["url"], "brands": [brand], "kind": "page"} for row in rows)
                diagnostic(db, "", "ok", f"{provider}: {query}. Найдено ссылок: {len(rows)}.")
            except (OSError, ValueError, KeyError, TypeError) as error:
                diagnostic(db, "", "error", f"Поиск {query}: {error}")
        with db:
            db.execute("INSERT OR REPLACE INTO web_state VALUES('query_cursor',?)",
                       (str((cursor + attempts) % len(queries)),))

    queue = deque(found)
    fetcher = Fetcher(config["request_timeout"], config["request_delay"])
    visited, pages = set(), []
    prior = [dict(json.loads(row["data"]), group_id=row["group_id"], content_hash=row["content_hash"])
             for row in db.execute("SELECT data,group_id,content_hash FROM web_documents ORDER BY first_seen,url")]
    while queue and len(visited) < config["max_pages"]:
        source = queue.popleft()
        url = source["url"]
        if url in visited:
            continue
        visited.add(url)
        try:
            final_url, raw = fetcher.get(url)
            if source["kind"] == "feed":
                queue.extend({"url": link, "brands": source["brands"], "kind": "page"}
                             for link in feed_links(final_url, raw, config["max_pages"]))
                diagnostic(db, url, "ok", "Лента или карта сайта обработана")
                continue
            if source["kind"] == "index":
                queue.extend({"url": link, "brands": source["brands"], "kind": "page"}
                             for link in index_links(final_url, raw, brands, 5))
            page = parse_page(final_url, raw, brands)
            if final_url in {entry["url"] for entry in pages}:
                continue
            group, content_hash = duplicate_group(page, prior + pages)
            page.update(group_id=group, content_hash=content_hash)
            pages.append(page)
            stamp = utc_now()
            previous = db.execute("SELECT first_seen FROM web_documents WHERE url=?", (final_url,)).fetchone()
            first = previous["first_seen"] if previous else stamp
            with db:
                db.execute("""
                    INSERT INTO web_documents VALUES(?,?,?,?,?,?)
                    ON CONFLICT(url) DO UPDATE SET checked=excluded.checked,group_id=excluded.group_id,
                      content_hash=excluded.content_hash,data=excluded.data
                """, (final_url, first, stamp, group, content_hash, json.dumps(page, ensure_ascii=False)))
                for product in page["products"]:
                    for name in page["brands"]:
                        if name not in _mentioned_product(product, brands):
                            continue
                        db.execute("""
                            INSERT INTO web_products VALUES(?,?,?,?,?,?)
                            ON CONFLICT(brand,name,url) DO UPDATE SET checked=excluded.checked,data=excluded.data
                        """, (name, product["name"], product["url"], first, stamp, json.dumps(product, ensure_ascii=False)))
            diagnostic(db, final_url, "ok", f"Бренды: {', '.join(page['brands'])}. Материал сохранён.")
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, etree.Error) as error:
            diagnostic(db, url, "error", str(error))
    if queue:
        diagnostic(db, "", "info", f"Достигнут лимит страниц за запуск: {config['max_pages']}.")
    try:
        classify_pages(db, pages, key)
    except (OSError, ValueError, TypeError, KeyError):
        diagnostic(db, "", "error", "Luna не завершила разметку интернет-материалов. Подтверждённые страницы сохранены.")
    if not key:
        diagnostic(db, "", "info", "Luna отключена. В отчёте использованы фактические признаки страниц.")


def _mentioned_product(product, brands):
    from internet_sources import brand_mentions
    return brand_mentions(product["name"], brands)


def promo_mechanic(text):
    text = normalize(text)
    for code, label in [("промокод", "Промокод"), ("подар", "Подарок"), ("набор", "Набор"),
                        ("упаков", "Предложение на упаковки"), ("скид", "Скидка"), ("%", "Скидка")]:
        if code in text:
            return label
    return "Промоматериал"


def snapshot(db, config):
    result = {name: [] for name in TABLES}
    brands = {brand["name"]: brand for brand in config["all_brands"]}
    groups, sites, questions, advertising, assortment = (defaultdict(set) for _ in range(5))
    question_seen = set()
    rows = db.execute("SELECT * FROM web_documents ORDER BY first_seen DESC,url").fetchall()
    for stored in rows:
        page = json.loads(stored["data"])
        labels = db.execute(
            "SELECT data FROM web_labels WHERE content_hash=? AND model='gpt-6-luna' AND version=1",
            (stored["content_hash"],)
        ).fetchone()
        label = json.loads(labels["data"]) if labels else {}
        title, domain = page["title"], urlsplit(page["url"]).hostname
        for name in page["brands"]:
            if name not in brands:
                continue
            group = stored["group_id"]
            groups[name].add(group)
            sites[name].add(domain)
            kind = label.get("kind", page["kind"])
            fields = [name, brands[name]["role"], title, kind, label.get("topic", ""),
                      label.get("audience", "Не указана"), label.get("message", ""),
                      page["published"], stored["first_seen"], stored["checked"], domain, group,
                      page["url"], "Luna" if label else "По странице"]
            result["Упоминания интернета"].append(fields)
            result["Материалы о продуктах"].append(
                [name, title, kind, *fields[4:8], stored["first_seen"], page["url"], fields[-1]]
            )
            for question in page["questions"]:
                explicit = brand_mentions(question, config["all_brands"])
                if (explicit and name not in explicit) or (not explicit and len(page["brands"]) != 1):
                    continue
                key = name, normalize(question)
                if key in question_seen:
                    continue
                question_seen.add(key)
                questions[name].add(normalize(question))
                result["Вопросы покупателей"].append([name, question, "Опубликованный вопрос / FAQ",
                                                       stored["first_seen"], page["url"]])
            for offer in page["advertisements"]:
                if name not in brand_mentions(offer, config["all_brands"]):
                    continue
                advertising[name].add((group, normalize(offer)))
                result["Реклама в интернете"].append(
                    [name, offer, promo_mechanic(offer), "Упомянута" if any(x in normalize(offer) for x in ("реклама", "erid"))
                     else "Не найдена в фрагменте", stored["checked"], page["url"]]
                )
    for row in db.execute("SELECT * FROM web_products ORDER BY first_seen DESC,brand,name"):
        if row["brand"] not in brands:
            continue
        product = json.loads(row["data"])
        assortment[row["brand"]].add(normalize(product["name"]))
        result["Ассортимент интернета"].append(
            [row["brand"], product["name"], product["forms"], product["amounts"], product["pack"],
             row["first_seen"], row["checked"], product["kind"], row["url"]]
        )
    for name, brand in brands.items():
        result["Интернет конкурентов"].append(
            [name, brand["role"], len(groups[name]), len(sites[name]), len(questions[name]),
             len(advertising[name]), len(assortment[name])]
        )
    for row in db.execute("SELECT * FROM web_search ORDER BY checked DESC,query,provider"):
        if row["brand"] not in brands:
            continue
        data = json.loads(row["data"])
        domain = urlsplit(row["url"]).hostname or ""
        official = any(domain == candidate or domain.endswith("." + candidate)
                       for candidate in brands[row["brand"]].get("domains", []))
        result["Поиск в интернете"].append([row["brand"], row["query"], row["provider"], row["region"],
                                           row["checked"], data.get("position"), data.get("depth"),
                                           data.get("title", ""), domain, "Да" if official else "Нет", row["url"]])
    result["Источники интернета"] = [
        [row["checked"], row["url"], STATUSES.get(row["status"], row["status"]), row["message"]]
        for row in db.execute("SELECT * FROM web_diagnostics ORDER BY id DESC LIMIT 200")
    ]
    return {"tables": result, "methodology": list(META), "generated_at": utc_now()}


def write_snapshot(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".json", delete=False, mode="w", encoding="utf-8") as handle:
        temporary = Path(handle.name)
        json.dump(report, handle, ensure_ascii=False, indent=2)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def add_excel_sheets(book, report, format_table):
    for name, headers in TABLES.items():
        sheet = book.create_sheet(name)
        sheet.append(headers)
        rows = report["tables"][name]
        for row in rows:
            sheet.append(row)
        widths = [40 if any(word in label.lower() for word in ("заголовок", "сообщение", "текст", "вопрос", "название"))
                  else 24 for label in headers]
        for column, label in enumerate(headers, 1):
            if label == "Источник":
                widths[column - 1] = 14
                for index in range(2, sheet.max_row + 1):
                    cell = sheet.cell(index, column)
                    if cell.value and str(cell.value).startswith(("https://", "http://")):
                        cell.hyperlink, cell.value = cell.value, "Открыть"
                        cell.style = "Hyperlink"
        format_table(sheet, widths)
        sheet.auto_filter.ref = sheet.dimensions


def main():
    parser = argparse.ArgumentParser(description="Сбор интернет-материалов по брендам")
    parser.add_argument("--config", type=Path, default=ROOT / "internet_config.json")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--no-search", action="store_true")
    parser.add_argument("--urls", type=Path)
    parser.add_argument("--search-csv", type=Path)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    try:
        config = load_config(args.config)
        report = collect(config, args.data_dir / "internet.sqlite3", no_ai=args.no_ai,
                         use_search=not args.no_search, url_import=args.urls, search_import=args.search_csv)
        write_snapshot(args.data_dir / "internet_snapshot.json", report)
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        parser.error(str(error))
    print("Подтверждённых упоминаний:", len(report["tables"]["Упоминания интернета"]))


if __name__ == "__main__":
    main()
