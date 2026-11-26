import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from lxml import html
from openpyxl import load_workbook
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_cards import archived_zdravcity_reviews, get_reviews


class RetailDashboardTests(unittest.TestCase):
    def test_product_json_reviews_survive_missing_or_incomplete_page_data(self):
        row = {"datePublished": "2026-11-24", "reviewBody": "Тест упаковки"}
        card = {"source": "Здравсити", "sku": "sample"}
        for data in ("{", "null", '{"props":{}}', json.dumps({
                "props": {"pageProps": {"productV2": {"reviews": [row]}}}})):
            document = html.fromstring(f'<html><script id="__NEXT_DATA__">{data}</script></html>')
            reviews = get_reviews(document, {"review": row}, card, "https://example.org/", date(2026,11,26), 10)
            self.assertEqual([r["body"] for r in reviews], ["Тест упаковки"])
        self.assertEqual(archived_zdravcity_reviews(date(2026,11,23)), [])

    def test_prices_and_reviews_when_zdravcity_denies_the_server(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PHARMA_DATA_DIR": tmp}):
            book = load_workbook(ROOT / "example/research_moscow_2026-11-24.xlsx")
            texts = book["Тексты отзывов"]
            for index in range(texts.max_row, 1, -1):
                if texts.cell(index, 3).value == "Здравсити":
                    texts.delete_rows(index)
            for row in list(book["Проверки"].rows)[1:]:
                if row[0].value == "Здравсити":
                    row[8].value = row[9].value = None
                    row[10].value, row[14].value = "Ошибка проверки", "HTTP 403"
            book.save(Path(tmp) / "prices_existing.xlsx")
            book.close()
            app = AppTest.from_file(str(ROOT / "dashboard.py"), default_timeout=30).run()
            self.assertFalse(app.exception, list(app.exception))
            tabs = {tab.label: tab for tab in app.tabs}
            self.assertFalse({"Диагностика", "Видимость"} & tabs.keys())
            prices = tabs["Цены"].dataframe[0].value
            self.assertEqual(prices[prices["Площадка"].eq("Apteka.ru")]["Цена за уп."].count(), 6)
            self.assertTrue(prices.loc[prices["Площадка"].eq("Здравсити"), "Цена за уп."].isna().all())
            source = tabs["Отзывы"].multiselect[0]
            source.set_value(["Здравсити"]).run()
            self.assertFalse(app.exception, list(app.exception))
            shown = next(t for t in app.tabs if t.label == "Отзывы").dataframe[0].value
            self.assertEqual(shown["Аптека"].tolist(), ["Здравсити"])
            self.assertTrue(shown["Источник"].iloc[0].startswith("https://zdravcity.ru/"))
