"""Run the CLI collector in an isolated process from the dashboard."""
import csv
import io
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TIMEOUT = 1200


@contextmanager
def collection_guard(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    with closing(sqlite3.connect(data_dir / 'dashboard_collection.sqlite3', timeout=10)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS collection_lock(id INTEGER PRIMARY KEY, token TEXT, expires REAL)')
        with db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT expires FROM collection_lock WHERE id=1').fetchone()
            if row and row[0] > time.time():
                raise ValueError('Общий сбор уже запущен. Дождитесь его завершения; после прерывания блокировка снимается в течение 21 минуты.')
            db.execute('INSERT OR REPLACE INTO collection_lock VALUES(1,?,?)', (token, time.time() + TIMEOUT + 60))
        try:
            yield
        finally:
            with db:
                db.execute('DELETE FROM collection_lock WHERE id=1 AND token=?', (token,))


def internet_settings(data_dir):
    from internet_monitor import load_config
    from internet_sources import canonical
    path = data_dir / 'internet_settings.json'
    settings = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    config = load_config(comparison=settings.get('comparison_brands'))
    for url in settings.get('urls', []):
        config['sources'].append({'url': canonical(url), 'kind': 'index',
                                  'brands': [brand['name'] for brand in config['all_brands']]})
    return config


def partner_template(catalog):
    from agent import FEED_FIELDS, PARTNER
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(FEED_FIELDS)
    for card in catalog["cards"]:
        if card["source"] == PARTNER:
            writer.writerow([PARTNER, card["sku"], catalog["city"], PARTNER, card["url"], "public", "", "", ""])
    return output.getvalue().encode("utf-8-sig")


def run_collection(data_dir, api_key='', use_api=False, include_internet=True, partner_feed=None):
    data_dir = Path(data_dir).resolve()
    with collection_guard(data_dir):
        stamp = datetime.now(timezone(timedelta(hours=3))).strftime('%Y-%m-%d_%H%M%S')
        output = data_dir / f'prices_{stamp}_{uuid.uuid4().hex[:8]}.xlsx'
        command = [sys.executable, str(ROOT / 'agent.py'), '--data-dir', str(data_dir), '--output', str(output)]
        if not use_api:
            command.extend(['--no-ai', '--no-internet-search'])
        if not include_internet:
            command.append('--skip-internet')
        temporary = None
        feed_path = None
        try:
            if partner_feed is not None:
                from agent import MSK, load_feed
                if not 0 < len(partner_feed) <= 2_000_000:
                    raise ValueError("Нужен непустой CSV размером до 2 МБ.")
                with tempfile.NamedTemporaryFile(dir=data_dir, suffix=".csv", delete=False) as handle:
                    feed_path = Path(handle.name)
                    handle.write(partner_feed)
                catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
                rows = load_feed(feed_path, catalog, datetime.now(MSK).date())
                if not rows or any(row["channel"] != "partner" for row in rows):
                    raise ValueError("Загрузите хотя бы одну строку согласованной выгрузки Здравсити.")
                command.extend(["--feed", str(feed_path)])
            if include_internet:
                config = internet_settings(data_dir)
                with tempfile.NamedTemporaryFile(mode='w', suffix='.json', prefix='collection_config_',
                                                 dir=data_dir, encoding='utf-8', delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(config, handle, ensure_ascii=False)
                command.extend(['--internet-config', str(temporary)])
            environment = dict(os.environ)
            if api_key:
                environment['OPENAI_API_KEY'] = api_key.strip()
            # Pass credentials only via the child's environment, never via its arguments.
            result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                                    text=True, encoding='utf-8', errors='replace', timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ValueError('Сбор превысил 20 минут и остановлен. Уменьшите число интернет-источников или повторите сбор без интернета.') from None
        except OSError:
            raise ValueError('Не удалось запустить сбор в окружении Streamlit.') from None
        finally:
            if feed_path is not None:
                feed_path.unlink(missing_ok=True)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if result.returncode not in {0, 2} or not output.exists() or output.stat().st_size == 0:
            detail = (result.stderr or result.stdout or '')[-6000:]
            for name, value in environment.items():
                if any(word in name.upper() for word in ('KEY', 'TOKEN', 'PASSWORD', 'SECRET')) and len(value) >= 4:
                    detail = detail.replace(value, '[скрыто]')
            logging.getLogger(__name__).error('Сбор завершился с кодом %s: %s', result.returncode, detail)
            raise ValueError('Сбор не создал новый отчёт. Проверьте доступность источников и журнал приложения Streamlit.')
        return {'path': output, 'partial': result.returncode == 2}
