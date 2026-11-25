import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard_collection import collection_guard, internet_settings, run_collection
from streamlit.testing.v1 import AppTest


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def fake_run(self, command, **kwargs):
        output = Path(command[command.index('--output') + 1])
        example = Path(__file__).resolve().parents[1] / 'example/research_moscow_2026-11-24.xlsx'
        shutil.copyfile(example, output)
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    def test_default_collection_disables_all_api_calls_and_cleans_config(self):
        with patch('dashboard_collection.subprocess.run', side_effect=self.fake_run) as run:
            result = run_collection(self.directory, api_key='private-key')
            command = run.call_args.args[0]
            self.assertIn('--no-ai', command)
            self.assertIn('--no-internet-search', command)
            self.assertNotIn('private-key', str(command))
            self.assertEqual(run.call_args.kwargs['env']['OPENAI_API_KEY'], 'private-key')
            self.assertTrue(result['path'].exists())
            self.assertFalse(list(self.directory.glob('collection_config_*.json')))

    def test_partial_report_is_still_returned(self):
        def partial(command, **kwargs):
            self.fake_run(command, **kwargs)
            return subprocess.CompletedProcess(command, 2, stdout='', stderr='')
        with patch('dashboard_collection.subprocess.run', side_effect=partial) as run:
            result = run_collection(self.directory, use_api=True, include_internet=False)
        self.assertTrue(result['partial'])
        self.assertIn('--skip-internet', run.call_args.args[0])
        self.assertNotIn('--no-ai', run.call_args.args[0])

    def test_settings_from_internet_tab_are_used(self):
        (self.directory/'internet_settings.json').write_text(json.dumps({'urls':['https://example.com/brand'],
            'comparison_brands':[{'name':'Другой бренд','aliases':['Другой бренд'],'role':'Для сравнения','domains':[]}]}))
        config = internet_settings(self.directory)
        self.assertIn('Другой бренд', [brand['name'] for brand in config['all_brands']])
        self.assertIn('https://example.com/brand', [source['url'] for source in config['sources']])

    def test_parallel_collection_is_rejected_and_lock_is_released(self):
        with collection_guard(self.directory):
            with self.assertRaises(ValueError):
                with collection_guard(self.directory):pass
        with collection_guard(self.directory):pass

    def test_timeout_releases_lock_and_removes_temporary_config(self):
        with patch('dashboard_collection.subprocess.run', side_effect=subprocess.TimeoutExpired(['agent.py'],1200)):
            with self.assertRaises(ValueError):run_collection(self.directory)
        self.assertFalse(list(self.directory.glob('collection_config_*.json')))
        with collection_guard(self.directory):pass

    def test_failed_process_does_not_select_an_old_report_or_log_key(self):
        old=self.directory/'prices_old.xlsx'; old.write_bytes(b'old')
        with patch('dashboard_collection.subprocess.run', return_value=subprocess.CompletedProcess([],1,'','failure secret-key')), \
                self.assertLogs('dashboard_collection', level='ERROR') as logs:
            with self.assertRaises(ValueError):run_collection(self.directory,api_key='secret-key')
        self.assertNotIn('secret-key',str(logs.output))

    def test_cli_accepts_output_and_disables_internet_search(self):
        import agent
        from internet_monitor import TABLES
        from openpyxl import load_workbook
        output=self.directory/'prices_custom.xlsx'
        report={'tables':{name:[] for name in TABLES},'methodology':[],'generated_at':'2026-11-25T12:00:00+00:00'}
        command=['agent.py','--data-dir',str(self.directory),'--output',str(output),'--no-ai','--no-internet-search']
        with patch.object(sys,'argv',command), patch.object(agent,'collect',return_value=[]), \
                patch.object(agent,'save_run',return_value=[]), \
                patch('market_research.run_research',return_value={}), patch('market_research.add_sheets'), \
                patch('internet_monitor.collect',return_value=report) as collect_internet:
            self.assertEqual(agent.main(),0)
        self.assertFalse(collect_internet.call_args.kwargs['use_search'])
        self.assertTrue(collect_internet.call_args.kwargs['no_ai'])
        self.assertTrue(output.exists())
        book=load_workbook(output,read_only=True)
        self.assertIn('Проверки',book.sheetnames)
        self.assertIn('Упоминания интернета',book.sheetnames)
        book.close()

    def test_sidebar_button_opens_the_new_report(self):
        root=Path(__file__).resolve().parents[1]
        with patch.dict(os.environ, {'PHARMA_DATA_DIR':str(self.directory)}), \
                patch('dashboard_collection.subprocess.run', side_effect=self.fake_run) as run:
            app=AppTest.from_file(str(root/'dashboard.py'),default_timeout=30).run()
            self.assertFalse(app.exception,list(app.exception))
            next(button for button in app.sidebar.button if button.label=='Собрать свежие данные').click().run()
            self.assertFalse(app.exception,list(app.exception))
            self.assertEqual(run.call_count,1)
            self.assertEqual(Path(app.sidebar.selectbox[0].value).parent,self.directory)
            self.assertTrue(app.success)
            self.assertFalse(any('Демонстрационный срез' in value.value for value in app.info))


if __name__=='__main__':unittest.main()
