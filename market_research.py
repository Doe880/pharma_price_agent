import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import timedelta
from urllib.parse import urlsplit

from lxml import etree

from research_ai import CONTACTS, MECHANICS, TOPICS, Luna
from research_cards import archived_zdravcity_reviews, audit_cards, digest, get_reviews, inspect_document, promo_items
from research_visibility import prepare_visibility, visibility_report


CARD_ACTIONS = {
    "price": "Уточнить условия цены и скидок в карточке.",
    "ease": "Проверить понятность сведений о форме выпуска и ссылки на инструкцию.",
    "packaging": "Вынести количество единиц и актуальное фото упаковки в основные характеристики.",
    "availability": "Проверить видимость региона, наличия и вариантов получения.",
    "choice": "Собрать вопросы о выборе для редакции FAQ и согласования формулировок.",
}


def read_retail_cards(catalog, pages, day, max_reviews, fetch_page):
    reviews, promotions, history, diagnostics = {}, [], [], []
    for card in catalog["cards"]:
        url, source, sku = (card[field] for field in ("url", "source", "sku"))
        try:
            if url not in pages:
                raise ValueError("Карточка не загрузилась")
            document, item = inspect_document(pages[url], card)
        except (ValueError, etree.ParserError) as exc:
            diagnostics.append(["Карточка", source, sku, str(exc), url])
            continue
        selected_document, selected_item, review_url = document, item, url
        if source == "Apteka.ru" and max_reviews > 6 and (item.get("review") or item.get("aggregateRating")):
            candidate = url.rstrip("/") + "/reviews/"
            try:
                selected_document, selected_item = inspect_document(fetch_page(candidate), card)
                review_url = candidate
            except (OSError, ValueError, etree.ParserError):
                diagnostics.append(["Отзывы", source, sku, "Страница отзывов недоступна. Взята выборка с карточки.", candidate])
        try:
            rows = get_reviews(selected_document, selected_item, card, review_url, day, max_reviews)
        except (ValueError, TypeError, AttributeError):
            rows = []
            diagnostics.append(["Отзывы", source, sku, "Формат отзывов изменился", review_url])
        for row in rows:
            row["body"] = CONTACTS.sub("[скрыто]", row["body"])
            reviews[row["id"]] = row
        aggregate = selected_item.get("aggregateRating") or {}
        diagnostics.append(["Отзывы", source, sku,
                            f"Получено текстов: {len(rows)}. Счётчик сайта: {aggregate.get('reviewCount', 'н/д')}.",
                            review_url])
        title = document.xpath("//title/text()")
        if catalog["city"] == "Москва" and title and "в москве" in title[0].casefold():
            found = promo_items(document, item, card)
            promotions.extend(found)
            diagnostics.append(["Промо", source, sku, f"В карточке зафиксировано механик: {len(found)}.", url])
        else:
            diagnostics.append(["Промо", source, sku, "Регион карточки не подтверждён. Промо пропущены.", url])
        history.append((url, (document, item)))
    return reviews, promotions, dict(history), diagnostics


def review_report(connection, reviews, luna, catalog, max_new, day, text_rows=None):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS review_samples(
          id TEXT PRIMARY KEY, sku TEXT, source TEXT, published TEXT, body TEXT, url TEXT
        )
    """)
    connection.executemany("""
        INSERT INTO review_samples VALUES (:id,:sku,:source,:date,:body,:url)
        ON CONFLICT(id) DO UPDATE SET url=excluded.url
    """, reviews.values())
    connection.commit()
    stored = [
        dict(zip(("id", "sku", "source", "date", "body", "url"), values))
        for values in connection.execute("""
            SELECT id, sku, source, published, body, url
            FROM review_samples WHERE published<=? ORDER BY published DESC, id
        """, (day.isoformat(),))
        if values[1] in catalog["products"]
    ]
    labels = luna.classify("review", {row["id"]: row["body"] for row in stored}, max_new)
    totals, counts, examples = defaultdict(int), defaultdict(int), defaultdict(list)
    items = []
    for row in stored:
        sku, key = row["sku"], row["id"]
        brand = catalog["products"][sku]["brand"]
        totals[sku] += 1
        tags = labels.get(key)
        if text_rows is not None:
            text_rows.append([
                brand, sku, row["source"], row["date"], CONTACTS.sub("[скрыто]", row["body"]),
                ", ".join(TOPICS[tag["code"]] for tag in tags) if tags else "",
                "Ожидает разметки" if tags is None else "Размечен Luna",
                row["url"],
            ])
        if tags is None:
            items.append([brand, sku, row["source"], row["date"], "Ожидает разметки", "", row["url"]])
            continue
        counts[sku] += 1
        if not tags:
            items.append([brand, sku, row["source"], row["date"], "Нет целевых тем", "", row["url"]])
        for tag in tags:
            topic, quote = tag["code"], tag["quote"]
            examples[sku, topic].append((quote, row["url"]))
            items.append([brand, sku, row["source"], row["date"], TOPICS[topic], quote, row["url"]])
    overview = []
    for sku, spec in catalog["products"].items():
        for topic, label in TOPICS.items():
            hits = examples[sku, topic]
            sample = list(dict.fromkeys(hits))[:2]
            overview.append([spec["brand"], sku, label, len(hits) if counts[sku] else None,
                             counts[sku], totals[sku], len(hits) / counts[sku] if counts[sku] else None,
                             " / ".join(quote for quote, _ in sample),
                             sample[0][1] if sample else "", sample[1][1] if len(sample) > 1 else "",
                             CARD_ACTIONS[topic] if hits else ""])
    return overview, items


def promo_report(connection, promotions, luna, catalog, checked, day):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS promo_samples(
          day TEXT, city TEXT, source TEXT, sku TEXT, promo TEXT, data TEXT,
          PRIMARY KEY(day, city, source, sku, promo)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS promo_coverage(
          day TEXT, city TEXT, source TEXT, sku TEXT, PRIMARY KEY(day, city, source, sku)
        )
    """)
    city, today = catalog["city"], day.isoformat()
    for source, sku in checked:
        connection.execute("DELETE FROM promo_samples WHERE day=? AND city=? AND source=? AND sku=?",
                           (today, city, source, sku))
        connection.execute("INSERT OR IGNORE INTO promo_coverage VALUES(?,?,?,?)", (today, city, source, sku))
    connection.executemany("INSERT OR REPLACE INTO promo_samples VALUES (?,?,?,?,?,?)", [
        (today, city, row["source"], row["sku"], row["key"], json.dumps(row, ensure_ascii=False))
        for row in promotions
    ])
    connection.commit()
    first = (day - timedelta(days=29)).isoformat()
    records = defaultdict(list)
    for date, data in connection.execute(
        "SELECT day,data FROM promo_samples WHERE city=? AND day<=? ORDER BY day", (city, today)
    ):
        row = json.loads(data)
        if row["sku"] in catalog["products"]:
            records[row["sku"], row["key"]].append((date, row))
    intents = {}
    ids = {}
    for key, values in records.items():
        if values[-1][0] < first:
            continue
        row = values[-1][1]
        message = f"{row['title']}. {row['terms']}."
        ids[key] = digest(message)
        intents[ids[key]] = message
    labels = luna.classify("promo", intents)
    calendar, by_card = [], defaultdict(list)
    for key, values in records.items():
        last_day, row = values[-1]
        if last_day < first:
            continue
        current, old = row["current"], row["original"]
        depth = (old - current) / old if old and current else None
        identity = row["source"], row["sku"]
        by_card[identity].append((values[0][0], {date for date, _ in values if date >= first}, depth))
        tag = labels.get(ids[key])
        mechanism = MECHANICS[tag[0]["code"]] if tag else f"{MECHANICS[row['site_kind']]} · по сайту"
        observed = ("Есть в сегодняшнем срезе" if last_day == today else
                    "Не видно в сегодняшнем срезе" if identity in checked else "Нет проверки сегодня")
        calendar.append([catalog["products"][row["sku"]]["brand"], row["sku"], row["source"], city,
                         row["title"], mechanism, current / 100 if current else None,
                         old / 100 if old else None, depth, row["terms"], values[0][0], last_day,
                         len({date for date, _ in values if date >= first}), observed,
                         row["url"]])
    summary = []
    for card in catalog["cards"]:
        identity = card["source"], card["sku"]
        coverage = connection.execute("""
            SELECT COUNT(*) FROM promo_coverage WHERE city=? AND source=? AND sku=? AND day BETWEEN ? AND ?
        """, (city, *identity, first, today)).fetchone()[0]
        offers = by_card[identity]
        active_days = set().union(*(dates for _, dates, _ in offers))
        depths = [depth for _, _, depth in offers if depth is not None]
        summary.append([catalog["products"][card["sku"]]["brand"], card["sku"], card["source"], city,
                        len(offers) if coverage else None, sum(start >= first for start, _, _ in offers)
                        if coverage else None, len(active_days) if coverage else None, coverage,
                        max(depths) if depths else None, first, today])
    return calendar, summary


def run_research(catalog, pages, database, day, no_ai, setup, fetch_page):
    config, visibility = setup
    reviews, promos, parsed, diagnostics = read_retail_cards(
        catalog, pages, day, config["review_limit_per_card"], fetch_page
    )
    for archived in archived_zdravcity_reviews(day):
        if archived["sku"] in catalog["products"]:
            reviews.setdefault(archived["id"], archived)
    checked = {(source, sku) for module, source, sku, message, _ in diagnostics
               if module == "Промо" and message.startswith("В карточке")}
    with closing(sqlite3.connect(database)) as connection, connection:
        luna = Luna(connection, no_ai)
        review_texts = []
        review_overview, review_items = review_report(
            connection, reviews, luna, catalog, config["max_ai_new_reviews"], day, review_texts
        )
        intent_map = {digest(query): query for query in config["query_targets"]}
        intent_labels = {intent_map[k]: val for k, val in luna.classify("query", intent_map).items()}
        overview, positions, losses = visibility_report(connection, config, visibility, intent_labels, day)
        calendar, frequency = promo_report(connection, promos, luna, catalog, checked, day)
        diagnostics.extend(["Luna", "", "", message, ""] for message in sorted(luna.diagnostics))
    differences = audit_cards(catalog, parsed, diagnostics)
    if not visibility and all(position[7] == "Нет среза" for position in positions):
        diagnostics.append(["Видимость", "Обе аптеки", "",
                            "Нет входной выдачи. Нужен CSV или сохранённый HTML из разрешённого источника.", ""])
    diagnostics.append(["Методика", "", "",
                        "Отзывы — накопленная выборка доступных карточек. Один отзыв может содержать несколько тем.", ""])
    diagnostics.append(["Методика", "", "",
                        "Промо — наблюдения витрин. Первая и последняя фиксация могут отличаться от срока акции.", ""])
    diagnostics.append(["Методика", "", "",
                        "Топ‑10 — найденные позиции или подтверждённый просмотр 10 карточек. Пустые срезы исключаются.", ""])
    return {
        "Отзывы по темам": review_overview, "Выборка отзывов": review_items,
        "Тексты отзывов": review_texts,
        "Видимость": overview, "Позиции поиска": positions, "Потери видимости": losses,
        "Аудит карточек": differences, "Календарь промо": calendar, "Частота промо": frequency,
        "Диагностика исследования": diagnostics,
    }


def add_sheets(book, research, format_table, money_format):
    sheets = [
        ("Тексты отзывов",
         ["Бренд", "SKU", "Аптека", "Дата отзыва", "Текст отзыва", "Темы", "Статус разметки", "Источник"],
         [16, 24, 16, 15, 100, 38, 23, 13], (8,), (), ()),
        ("Отзывы по темам",
         ["Бренд", "SKU", "Тема", "Отзывов с темой", "Размечено", "Всего в базе", "Доля", "Примеры цитат",
          "Источник 1", "Источник 2", "Что проверить в карточке или FAQ"],
         [16, 21, 23, 15, 13, 14, 12, 64, 13, 13, 40], (9, 10), (), (7,)),
        ("Выборка отзывов",
         ["Бренд", "SKU", "Аптека", "Дата отзыва", "Тема или статус", "Цитата", "Источник"],
         [16, 21, 16, 15, 25, 64, 13], (7,), (), ()),
        ("Видимость", ["Аптека", "Город", "SKU", "В топ‑10", "Проверено", "Всего запросов", "Доля топ‑10",
                       "Сопоставимых со вчера", "Изменение, п.п."],
         [17, 16, 23, 13, 13, 16, 15, 21, 19], (), (), (7,)),
        ("Позиции поиска", ["Аптека", "Город", "SKU", "Запрос", "Намерение Luna", "Позиция", "Глубина",
                           "Статус", "Время МСК", "Выдача"],
         [17, 16, 23, 34, 23, 12, 12, 25, 28, 14], (10,), (), ()),
        ("Потери видимости", ["Аптека", "Город", "SKU", "Запрос", "Позиция вчера", "Позиция сейчас", "Изменение", "Выдача"],
         [17, 16, 23, 34, 16, 17, 44, 14], (8,), (), ()),
        ("Аудит карточек", ["Бренд", "SKU", "Приоритет", "Поле", "Аптека A", "Значение A", "Аптека Б",
                          "Значение Б / эталон", "Источник A", "Источник Б", "Что проверить"],
         [16, 21, 14, 19, 16, 44, 16, 44, 13, 13, 46], (9, 10), (), ()),
        ("Календарь промо", ["Бренд", "SKU", "Аптека", "Город", "Название", "Механика", "Цена",
                           "Зачёркнутая цена", "Скидка", "Условия цены", "Впервые замечена", "Последний раз",
                           "Дней за 30", "Статус среза", "Источник"],
         [15, 21, 16, 16, 52, 28, 15, 18, 12, 41, 18, 16, 14, 29, 14], (15,), (7, 8), (9,)),
        ("Частота промо", ["Бренд", "SKU", "Аптека", "Город", "Уникальных промо", "Впервые за 30 дней",
                          "Дней с промо", "Проверенных дней", "Макс. скидка", "Начало окна", "Конец окна"],
         [16, 21, 16, 16, 19, 20, 16, 19, 16, 15, 15], (), (), (9,)),
        ("Диагностика исследования", ["Модуль", "Аптека", "SKU", "Комментарий", "Источник"],
         [17, 17, 21, 83, 14], (5,), (), ()),
    ]
    for title, headers, widths, links, money_columns, percent_columns in sheets:
        sheet = book.create_sheet(title)
        sheet.append(headers)
        for row in research[title]:
            sheet.append(row)
            index = sheet.max_row
            for col in links:
                cell = sheet.cell(index, col)
                if cell.value and urlsplit(cell.value).scheme == "https":
                    cell.hyperlink = cell.value
                    cell.value = "Открыть"
                    cell.style = "Hyperlink"
            for col in money_columns:
                sheet.cell(index, col).number_format = money_format
            for col in percent_columns:
                sheet.cell(index, col).number_format = "0.0%"
            if title == "Видимость":
                sheet.cell(index, 9).number_format = '+0.0;-0.0;0.0'
        format_table(sheet, widths)
        for index, row in enumerate(research[title], 2):
            if any(isinstance(value, str) and len(value) > 85 for col, value in enumerate(row, 1) if col not in links):
                sheet.row_dimensions[index].height = 52
        sheet.auto_filter.ref = sheet.dimensions
