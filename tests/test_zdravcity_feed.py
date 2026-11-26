import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent
from dashboard_collection import partner_template, run_collection


class ZdravcityFeedTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.catalog = json.loads((agent.ROOT / "catalog.json").read_text())
        template = partner_template(self.catalog).decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(template), delimiter=";"))
        self.assertEqual(len(rows), 5)
        self.row = dict(rows[0], price_rub="12,34", availability="in_stock",
                        observed_at=datetime.now(agent.MSK).isoformat())
        self.day = datetime.fromisoformat(self.row["observed_at"]).date()

    def csv_file(self, *rows, delimiter=";"):
        path = self.root / "input.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=agent.FEED_FIELDS, delimiter=delimiter)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_fallback_to_verified_sku_preserves_diagnostic_and_time(self):
        for delimiter in (";", ","):
            imports = agent.load_feed(self.csv_file(self.row, delimiter=delimiter), self.catalog, self.day)
            failed = dict(imports[0], state="error", channel="site", kopecks=None, error="HTTP 403")
            current = agent.apply_feed([failed], imports)
            self.assertEqual(current, [dict(imports[0], error=f'HTTP 403 · попытка {failed["observed_at"]}')])
            self.assertEqual(current[0]["kopecks"], 1234)
            history = agent.save_run(self.root / "prices.sqlite3", delimiter, current)
            overview, _ = agent.summarize(self.catalog, current, history, self.day)
            self.assertEqual(overview[0]["min_rub"], 12.34)
            for state in ("in_stock", "out_of_stock", "unknown"):
                site = dict(failed, state=state, error="")
                self.assertEqual(agent.apply_feed([site], imports), [site])
            self.assertEqual(agent.apply_feed([failed], []), [failed])

    def test_rejects_wrong_card_stale_observation_and_duplicate(self):
        yesterday = datetime.fromisoformat(self.row["observed_at"]) - timedelta(days=1)
        for rows in ([dict(self.row, url="https://zdravcity.ru/another")],
                     [dict(self.row, observed_at=yesterday.isoformat())],
                     [dict(self.row, availability="out_of_stock")], [self.row, self.row]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                agent.load_feed(self.csv_file(*rows), self.catalog, self.day)

    def test_403_records_only_selected_response_metadata(self):
        exc = HTTPError(self.row["url"], 403, "Forbidden",
                        {"X-Request-ID": "abc-123", "Set-Cookie": "sensitive"}, None)
        card = next(c for c in self.catalog["cards"] if c["url"] == self.row["url"])
        with patch.object(agent, "fetch_html", side_effect=exc):
            row, = agent.collect(dict(self.catalog, cards=[card]))
        self.assertEqual(row["state"], "error")
        self.assertIsNone(row["kopecks"])
        self.assertIn("HTTP 403", row["error"])
        self.assertIn("abc-123", row["error"])
        self.assertNotIn("sensitive", row["error"])

    def test_configured_feed_requested_once_after_denial_only(self):
        imports = agent.load_feed(self.csv_file(self.row), self.catalog, self.day)
        failed = dict(imports[0], channel="site", state="error", error="HTTP 403", kopecks=None)
        with patch.dict(os.environ, {"ZDRAVCITY_FEED_URL": "https://example.org/feed",
                                     "ZDRAVCITY_FEED_TOKEN": "secret"}), patch.object(agent, "build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(self.csv_file(self.row).read_bytes())
            self.assertEqual(agent.configured_feed([dict(failed, error="")], [], self.catalog, self.day), [])
            self.assertEqual(agent.configured_feed([failed], imports, self.catalog, self.day), [])
            opener.assert_not_called()
            result = agent.configured_feed([failed], [], self.catalog, self.day)
            self.assertEqual(result, imports)
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer secret")
            self.assertEqual(agent.apply_feed([failed], result)[0]["kopecks"], 1234)

    def test_bad_remote_feed_keeps_failure_and_hides_credentials(self):
        failed = {"source": agent.PARTNER, "sku": self.row["sku"], "error": "HTTP 403"}
        with patch.dict(os.environ, {"ZDRAVCITY_FEED_URL": "https://example.org/?secret=q"}), \
                patch.object(agent, "build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(b"<html>oops</html>")
            self.assertEqual(agent.configured_feed([failed], [], self.catalog, self.day), [])
        self.assertIn("выгрузка недоступна", failed["error"])
        self.assertNotIn("secret", failed["error"])

    def test_upload_is_validated_and_removed_after_collection(self):
        def collect(command, **kwargs):
            self.temporary = Path(command[command.index("--feed") + 1])
            self.assertTrue(self.temporary.exists())
            Path(command[command.index("--output") + 1]).write_bytes(b"report")
            return subprocess.CompletedProcess(command, 2)

        with patch("dashboard_collection.subprocess.run", side_effect=collect) as run:
            run_collection(self.root, partner_feed=self.csv_file(self.row).read_bytes(), include_internet=False)
            self.assertFalse(self.temporary.exists())
            with self.assertRaises(ValueError):
                run_collection(self.root, partner_feed=b"bad header", include_internet=False)
            self.assertEqual(run.call_count, 1)
