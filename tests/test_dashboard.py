import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard_summary


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

    def test_cached_four_argument_payload_keeps_internet_metrics(self):
        root = Path(__file__).resolve().parents[1]
        current_builder = dashboard_summary.build_payload
        payloads = []

        def legacy_builder(heading, selected_skus, city, frames):
            return current_builder(heading, selected_skus, city, frames)

        def read_cached_summary(path, payload):
            payloads.append(json.loads(payload))
            return None

        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, {'PHARMA_DATA_DIR': data_dir}), \
                patch.object(dashboard_summary, 'build_payload', legacy_builder), \
                patch.object(dashboard_summary, 'cached_summary', read_cached_summary):
            app = AppTest.from_file(str(root/'dashboard.py'), default_timeout=30).run()
            self.assertFalse(app.exception, list(app.exception))
            self.assertTrue(payloads)
            self.assertIn('интернет', payloads[-1])
            self.assertTrue(payloads[-1]['интернет']['показатели'])
            app.sidebar.multiselect[0].set_value(['hepiklim-forte-645-30']).run()
            self.assertFalse(app.exception, list(app.exception))
            rows = payloads[-1]['интернет']['показатели']
            self.assertEqual([row['Бренд'] for row in rows], ['Хепиклим форте'])


if __name__ == '__main__':
    unittest.main()
