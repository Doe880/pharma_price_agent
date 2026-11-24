import csv
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from lxml import html

from research_ai import INTENTS

SOURCES = {
    "Apteka.ru": {"apteka.ru", "www.apteka.ru"},
    "Здравсити": {"zdravcity.ru", "www.zdravcity.ru"},
}
STATUS = {"found": "Найден", "not_found": "Вне просмотренной выдачи", "error": "Ошибка"}
PRODUCT_ID = re.compile(r"(?:-[a-f0-9]{24}/?$|-\d{7}\.html$)", re.I)


def checked_url(url, source):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in SOURCES[source] or not parts.path.strip("/"):
        raise ValueError("Ссылка должна вести на HTTPS-страницу указанной аптеки")
    return parts


def clean(value):
    return " ".join(str(value).casefold().replace("ё", "е").split())


def initial_city(document):
    decoder = json.JSONDecoder()
    for script in document.xpath('//script[contains(text(),"__INITIAL_STATE__")]/text()'):
        match = re.search(r"window\.__INITIAL_STATE__\s*=\s*", script)
        if not match:
            continue
        try:
            state, _ = decoder.raw_decode(script[match.end():])
            return state.get("selectedCity", {}).get("name", "")
        except (ValueError, AttributeError):
            continue
    return ""


def search_page(source, city, query, result_url, raw, cards):
    parts = checked_url(result_url, source)
    document = html.fromstring(raw)
    if source == "Здравсити":
        if clean(parse_qs(parts.query).get("what", [""])[0]) != clean(query):
            raise ValueError("Поисковый запрос не совпадает с URL выдачи")
        area = document.xpath('//*[contains(@class,"DeliveryAddress_address-value")]/text()')
        region = clean(area[0]) if area else ""
        if region not in {clean(city), clean(city + " и область")}:
            raise ValueError("Город не подтверждён в сохранённой выдаче")
        links = document.xpath(
            '//a[@href and (contains(@class,"Card_card-inner-content-title") '
            'or contains(@class,"Card_card-title") or contains(@class,"Card_card-inner-content-name"))]/@href'
        )
        if not links:
            # Cards with two links (image and title) share the same product path.
            links = document.xpath(
                '//*[contains(@class,"Products_products") or contains(@class,"Result_result") '
                'or contains(@class,"ProductList_product-list") or contains(@class,"Catalog_catalog")]'
                '//a[starts-with(@href,"/p_")]/@href'
            )
    else:
        parameters = parse_qs(parts.query)
        candidates = parameters.get("q", []) + parameters.get("search", []) + parameters.get("query", [])
        if clean(query) not in {clean(value) for value in candidates}:
            raise ValueError("Поисковый запрос не подтверждён в URL выдачи")
        area = document.xpath(
            '//*[contains(@class,"LocationButton") or contains(@class,"RegionSelect") '
            'or contains(@class,"CitySelector") or contains(@class,"CurrentCity")]/text()'
        )
        area.append(initial_city(document))
        if clean(city) not in {clean(value) for value in area}:
            raise ValueError("Город не подтверждён в сохранённой выдаче")
        links = document.xpath(
            '//*[contains(@class,"SearchResult") or contains(@class,"SearchPage") '
            'or contains(@class,"CardsList")]//a[contains(@href,"/product/")]/@href'
        )

    paths = []
    for link in links:
        parts = urlsplit(urljoin(result_url, link))
        path = parts.path.rstrip("/")
        if parts.hostname in SOURCES[source] and PRODUCT_ID.search(path) and path not in paths:
            paths.append(path)
    if not paths:
        raise ValueError("В сохранённой выдаче не распознан список товаров. Используйте CSV позиций.")
    targets = {}
    for card in cards:
        if card["source"] == source:
            path = urlsplit(card["url"]).path.rstrip("/")
            targets[card["sku"]] = paths.index(path) + 1 if path in paths else None
    return targets, len(paths)


def read_rows(path, fields):
    if path is None:
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not set(fields) <= set(reader.fieldnames or ()):
            raise ValueError(f"{path.name} — нужны столбцы {', '.join(fields)}")
        rows = []
        for row in reader:
            if None in row or any(row[field] is None for field in fields):
                raise ValueError(f"{path.name}, строка {reader.line_num}. Неверное число полей.")
            rows.append((path, reader.line_num, {field: row[field].strip() for field in fields}))
        return rows


def prepare_visibility(config_path, catalog, day, feed_path, snapshots_path, now):
    config = json.loads(config_path.read_text(encoding="utf-8"))
    queries = {}
    cities = config["cities"]
    if not isinstance(cities, list) or not cities or any(not isinstance(city, str) or not city.strip() for city in cities):
        raise ValueError("Укажите список городов в research_config.json")
    if len(cities) != len(set(cities)):
        raise ValueError("В конфигурации повторяются города")
    for item in config["queries"]:
        query, targets = item["query"], item["targets"]
        if not isinstance(query, str) or not query.strip() or query in queries or not targets:
            raise ValueError("У каждого поискового запроса должно быть уникальное имя и список SKU")
        if len(targets) != len(set(targets)) or not set(targets) <= set(catalog["products"]):
            raise ValueError(f"Запрос {query} содержит повтор или неизвестный SKU")
        queries[query] = targets
    if not queries:
        raise ValueError("Добавьте хотя бы один поисковый запрос")
    for key, high in [("review_limit_per_card", 200), ("max_ai_new_reviews", 1000),
                      ("coverage_threshold_pct", 100)]:
        if type(config[key]) is not int or not 1 <= config[key] <= high:
            raise ValueError(f"{key} должно быть целым числом от 1 до {high}")

    common = ("source", "city", "query", "observed_at", "result_url")
    raw = read_rows(feed_path, (*common, "sku", "position", "depth", "status"))
    raw.extend(read_rows(snapshots_path, (*common, "html_path")))
    accepted = []
    identities = set()
    examinations = {}
    for file, number, row in raw:
        try:
            source, city, query = (row[k] for k in ("source", "city", "query"))
            if source not in SOURCES or city not in cities or query not in queries:
                raise ValueError("Площадка, город или запрос отсутствуют в конфигурации")
            checked_url(row["result_url"], source)
            stamp = datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.astimezone(now.tzinfo).date() != day:
                raise ValueError("Нужна текущая дата Москвы и часовой пояс")
            if stamp > now + timedelta(minutes=5):
                raise ValueError("Время наблюдения в будущем")
            row["observed_at"] = stamp.astimezone(now.tzinfo).isoformat()
            group = source, city, query
            examination = row["observed_at"], row["result_url"], "html" if "html_path" in row else "csv"
            if group in examinations and examinations[group] != examination:
                raise ValueError("Для одного запроса, города и площадки нужен один срез за запуск")
            examinations[group] = examination
            if "html_path" in row:
                path = file.parent / row.pop("html_path")
                if path.stat().st_size > 15_000_000:
                    raise ValueError("HTML больше 15 МБ")
                positions, depth = search_page(source, city, query, row["result_url"],
                                               path.read_text(encoding="utf-8-sig"), catalog["cards"])
                records = [dict(row, sku=sku, position=positions.get(sku), depth=depth,
                                status="found" if positions.get(sku) else "not_found")
                           for sku in queries[query]]
            else:
                if row["sku"] not in queries[query] or row["status"] not in STATUS:
                    raise ValueError("Неизвестные SKU или статус")
                position = int(row["position"]) if row["position"] else None
                depth = int(row["depth"])
                status = row["status"]
                if (
                    depth < 0 or depth > 10000 or (status == "found" and not 1 <= (position or 0) <= depth)
                    or (status != "found" and position is not None)
                    or (status == "error" and depth != 0)
                    or (status == "not_found" and depth == 0)
                ):
                    raise ValueError("Позиция, глубина и статус не согласуются")
                records = [dict(row, position=position, depth=depth)]
            for record in records:
                key = (*group, record["sku"])
                if key in identities:
                    raise ValueError("Дубликат площадки, города, запроса и SKU")
                identities.add(key)
                accepted.append(dict(record, channel=examination[2]))
        except (ValueError, OSError, KeyError) as exc:
            raise ValueError(f"{file.name}, строка {number}. {exc}") from exc
    groups = defaultdict(list)
    for row in accepted:
        groups[(row["source"], row["city"], row["query"])].append(row)
    for group, rows in groups.items():
        ranked = [row["position"] for row in rows if row["position"] is not None]
        if len({row["depth"] for row in rows}) != 1 or len(set(ranked)) != len(ranked):
            raise ValueError(f"Неодинаковая глубина или повтор позиции в срезе {group}")
        if any(row["status"] == "error" for row in rows) and not all(row["status"] == "error" for row in rows):
            raise ValueError(f"Ошибку загрузки нужно указать для всех строк среза {group}")
    config["query_targets"] = queries
    return config, accepted


def measurable(row):
    return row["status"] == "found" or (row["status"] == "not_found" and row["depth"] >= 10)


def group_key(row):
    return row["source"], row["city"], row["query"], row["sku"]


def visibility_report(connection, config, collected, labels, day):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS search_samples(
            day TEXT, source TEXT, city TEXT, query TEXT, sku TEXT, observed TEXT, data TEXT,
            PRIMARY KEY(day, source, city, query, sku)
        )
    """)
    connection.executemany("""
        INSERT INTO search_samples VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(day, source, city, query, sku) DO UPDATE SET
          observed=excluded.observed, data=excluded.data WHERE excluded.observed>=search_samples.observed
    """, [(day.isoformat(), *group_key(row), row["observed_at"], json.dumps(row, ensure_ascii=False))
          for row in collected])
    previous_day = (day - timedelta(days=1)).isoformat()
    history = {
        key: {group_key(row): row for row in (json.loads(record[0]) for record in connection.execute(
            "SELECT data FROM search_samples WHERE day=?", (key,)
        ))}
        for key in (day.isoformat(), previous_day)
    }
    today, previous = history[day.isoformat()], history[previous_day]
    summary, detail, losses = [], [], []
    for source in SOURCES:
        for city in config["cities"]:
            for sku in sorted({sku for targets in config["query_targets"].values() for sku in targets}):
                tracked = [q for q, targets in config["query_targets"].items() if sku in targets]
                paired = []
                hits = checked = 0
                for query in tracked:
                    key = source, city, query, sku
                    row = today.get(key)
                    old = previous.get(key)
                    label = labels.get(query)
                    intent = INTENTS[label[0]["code"]] if label else "Не размечено"
                    if row and measurable(row):
                        checked += 1
                        hits += bool(row["position"] and row["position"] <= 10)
                        if old and measurable(old):
                            paired.append((bool(old["position"] and old["position"] <= 10),
                                           bool(row["position"] and row["position"] <= 10)))
                            if old["position"] and old["position"] <= 10 and (
                                not row["position"] or row["position"] > 10
                            ):
                                losses.append([source, city, sku, query, old["position"], row["position"],
                                               "Выход из топ‑10", row["result_url"]])
                    if row:
                        detail.append([source, city, sku, query, intent, row["position"], row["depth"],
                                       STATUS[row["status"]], row["observed_at"], row["result_url"]])
                    else:
                        detail.append([source, city, sku, query, intent, None, None, "Нет среза", "", ""])
                change = None
                if paired:
                    change = (sum(now - old for old, now in paired)) / len(paired)
                    if change * 100 <= -config["coverage_threshold_pct"]:
                        losses.append([source, city, sku, "", None, None,
                                       f"Доля топ‑10: {change * 100:+.0f} п.п. ({len(paired)} сопоставимых запросов)", ""])
                summary.append([source, city, sku, hits, checked, len(tracked),
                                hits / checked if checked else None, len(paired),
                                change * 100 if change is not None else None])
    return summary, detail, losses
