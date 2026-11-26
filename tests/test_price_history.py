import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from price_history import last_zdravcity_checks


class PriceHistoryTests(unittest.TestCase):
    def test_latest_matching_prior_observation_including_out_of_stock(self):
        reference = pd.read_csv(ROOT / "example/zdravcity_2026-11-26.csv")
        failed = reference.assign(**{"Товар": reference["Препарат"], "Наличие": "Ошибка проверки",
                                     "Время МСК": pd.Timestamp("2026-11-26 11:40")})
        card = reference.iloc[[0]]
        newer = card.assign(**{"Время МСК": "2026-11-26 11:39:00", "Цена за уп.": 100})
        future = card.assign(**{"Время МСК": "2026-11-26 11:41:00", "Цена за уп.": 101})
        history = pd.concat([newer, future, future.assign(Город="Тула")])
        display = last_zdravcity_checks(failed, history, reference)
        self.assertEqual(len(display), 5)
        latest = display[display["Карточка"] == card["Карточка"].iloc[0]].iloc[0]
        self.assertEqual(latest["Цена за уп."], 100)
        self.assertEqual(latest["Источник данных"], "История выбранного отчёта")
        unavailable = display[display["Наличие"] == "Нет в наличии"]
        self.assertEqual(len(unavailable), 2)
        self.assertTrue(unavailable["Цена за уп."].isna().all())
        older = failed.assign(**{"Время МСК": pd.Timestamp("2026-11-25 12:00")})
        self.assertTrue(last_zdravcity_checks(older, history, reference).empty)
