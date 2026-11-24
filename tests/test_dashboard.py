import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


class DashboardTests(unittest.TestCase):
    def test_hepiklim_filter_and_internet_sections(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, {'PHARMA_DATA_DIR': data_dir}):
            app = AppTest.from_file(str(root/'dashboard.py'), default_timeout=30).run()
            self.assertFalse(app.exception, list(app.exception))
            self.assertIn('Интернет', [tab.label for tab in app.tabs])
            self.assertIn('Сравнение брендов', [tab.label for tab in app.tabs])
            app.sidebar.multiselect[0].set_value(['hepiklim-forte-645-30']).run()
            self.assertFalse(app.exception, list(app.exception))
            self.assertTrue(any('Хепиклим форте' in str(frame.value) for frame in app.dataframe))
            self.assertTrue(any(metric.label == 'Карточек проверено' and metric.value == '1' for metric in app.metric))


if __name__ == '__main__':
    unittest.main()
