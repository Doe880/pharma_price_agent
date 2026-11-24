import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from internet_channels import get_json, serp_results, social_page, source_category, threads_results
from internet_monitor import collect, load_config, open_database
from internet_search import discovery_queries
from internet_sources import parse_page


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_query_rotation_includes_all_brands_aliases_and_channels(self):
        queries = discovery_queries(self.config['all_brands'], self.config['channels'])
        self.assertNotIn('site:', queries[0][1])
        self.assertIn('site:threads.com', queries[1][1])
        self.assertIn('site:threads.net', queries[1][1])
        self.assertIn('site:pharmvestnik.ru', queries[2][1])
        for channel in self.config['channels']:
            for brand in self.config['all_brands']:
                self.assertTrue(any(name == brand['name'] and 'site:' + channel['domains'][0] in query
                                    for name, query in queries))
        self.assertTrue(any('Amulapro' in query and 'Амула Про' in query for _,query in queries))

    def test_google_yandex_keep_provider_ranks_and_separate_parameters(self):
        response = {'search_metadata': {'status': 'Success'}, 'organic_results': [
            {'position': 1, 'title': 'A', 'link': 'https://example.com/a'},
            {'position': 2, 'title': 'Bad', 'link': 'http://127.0.0.1/'},
            {'position': 3, 'title': 'B', 'link': 'https://example.com/b'},
        ]}
        for engine in ['google','yandex']:
            with patch('internet_channels.get_json', return_value=response) as request:
                rows, depth = serp_results('Галавит','secret',engine,'RU','Москва',213)
                self.assertEqual([row['position'] for row in rows], [1,3])
                self.assertEqual(depth,3)
                url = request.call_args.args[0]
                self.assertIn('engine='+engine,url)
                self.assertIn('q=' if engine=='google' else 'text=',url)
                if engine=='yandex':self.assertIn('lr=213',url)
        with patch('internet_channels.get_json', return_value={'error':'secret'}):
            with self.assertRaises(ValueError) as error:serp_results('x','secret','google','RU','Москва')
            self.assertNotIn('secret',str(error.exception))

    def test_threads_filters_domains_and_preserves_short_verified_post(self):
        value={'data':[
            {'text':'Галавит где купить?', 'permalink':'https://www.threads.com/@u/post/abc','timestamp':'2025-01-01T12:00:00+0000'},
            {'text':'Галавит', 'permalink':'https://evil.example/post/abc','timestamp':'2025-01-01'},
        ]}
        with patch('internet_channels.get_json', return_value=value) as request:
            rows=threads_results('Галавит','private-token')
        self.assertIn('graph.threads.com/v1.0/keyword_search',request.call_args.args[0])
        self.assertEqual(len(rows),1)
        page=social_page(rows[0],self.config['all_brands'])
        self.assertEqual(page['published'],'2025-01-01')
        self.assertEqual(page['brands'],['Галавит'])
        self.assertEqual(page['questions'],['Галавит где купить?'])
        self.assertEqual(page['origin'],'Threads API')

    def test_provider_network_errors_do_not_disclose_query_credentials(self):
        with patch('internet_channels.urlopen',side_effect=OSError('https://server?api_key=secret')):
            with self.assertRaises(ValueError) as error:get_json('https://example.com/?api_key=secret','Test')
        self.assertNotIn('secret',str(error.exception))

    def test_channel_domains_cannot_match_lookalikes(self):
        channels=self.config['channels']
        self.assertEqual(source_category('https://pharmvestnik.ru/news/x',channels),('Отраслевые СМИ','Фармвестник'))
        self.assertEqual(source_category('https://www.threads.com/@u/post/a',channels),('Соцсети','Threads'))
        self.assertEqual(source_category('https://pharmvestnik.ru.evil.example/x',channels)[0],'Другие источники')

    def test_budget_rotation_and_threads_ingestion_without_html_fetch(self):
        config=copy.deepcopy(self.config)
        config['sources']=[]
        config['max_discovery_queries']=3
        config['max_threads_queries']=2
        config['search_providers']=['google','yandex']
        posts=[{'url':'https://www.threads.com/@u/post/a','text':'Галавит где купить?','published':'2025-01-01'}]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'SERPAPI_API_KEY':'secret','THREADS_ACCESS_TOKEN':'token'},clear=True), \
                patch('internet_monitor.serp_results',return_value=([],0)) as search, \
                patch('internet_monitor.threads_results',return_value=posts) as threads, \
                patch('internet_monitor.Fetcher.get') as fetch:
            path=Path(directory)/'internet.sqlite3'
            result=collect(config,path,no_ai=True)
            self.assertEqual(search.call_count,3)
            self.assertEqual([call.args[2] for call in search.call_args_list],['google','yandex','google'])
            self.assertEqual([call.args[0] for call in threads.call_args_list],['Галавит','Амула Про'])
            fetch.assert_not_called()
            self.assertEqual(len(result['tables']['Упоминания интернета']),1)
            self.assertEqual(result['tables']['Вопросы покупателей'][0][2],'Пост Threads')
            self.assertNotIn('secret',json.dumps(result))
            collect(config,path,no_ai=True)
            self.assertEqual(search.call_args_list[3].args[2],'yandex')
            with open_database(path) as db:
                self.assertEqual(db.execute("SELECT value FROM web_state WHERE key='query_cursor_v2'").fetchone()[0],'6')

    def test_seed_queue_rotates_under_small_page_budget(self):
        config=copy.deepcopy(self.config)
        config['max_pages']=1
        config['sources']=[{'url':'https://example.com/a','brands':['Галавит'],'kind':'page'},
                           {'url':'https://example.com/b','brands':['Галавит'],'kind':'page'}]
        with tempfile.TemporaryDirectory() as directory, patch('internet_monitor.Fetcher.get',side_effect=ValueError('unavailable')) as fetch:
            path=Path(directory)/'web.sqlite3'
            collect(config,path,no_ai=True,use_search=False)
            collect(config,path,no_ai=True,use_search=False)
            self.assertEqual([call.args[0] for call in fetch.call_args_list],['https://example.com/a','https://example.com/b'])

    def test_threads_login_page_is_not_a_confirmed_mention(self):
        raw='<html><head><title>Галавит в Threads</title></head><body><main>' + 'Войдите, чтобы продолжить. '*8 + '</main></body></html>'
        with self.assertRaises(ValueError):
            parse_page('https://www.threads.com/@u/post/a',raw,self.config['all_brands'])

    def test_no_search_flag_never_calls_external_apis(self):
        config=copy.deepcopy(self.config); config['sources']=[]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'SERPAPI_API_KEY':'key','THREADS_ACCESS_TOKEN':'token'}), \
                patch('internet_monitor.serp_results') as search, patch('internet_monitor.threads_results') as threads:
            collect(config,Path(directory)/'web.sqlite3',no_ai=True,use_search=False)
            search.assert_not_called(); threads.assert_not_called()


if __name__=='__main__':unittest.main()
