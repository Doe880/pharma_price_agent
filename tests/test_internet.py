import copy
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from internet_monitor import TABLES, collect, import_search, load_config, open_database
from internet_search import openai_sources, classify_pages
from internet_sources import canonical, parse_page, duplicate_group, public_url
from dashboard_summary import build_payload
from product_utils import matches_name

HTML = '''<html><head><title>Хепиклим форте — вопросы</title>
<meta property="article:published_time" content="2025-05-01"></head><body><main>
<h1>Хепиклим форте</h1><p>Информация об упаковке и наличии продукта Хепиклим форте для посетителей сайта.</p>
<h2>Как найти Хепиклим форте в аптеке?</h2><p>Акция на Хепиклим форте: скидка по промокоду.</p>
<h3>Хепиклим форте 645 мг 30 капсул</h3></main></body></html>'''

class InternetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.config = load_config()

    def test_catalog_and_visibility_have_hepiklim(self):
        root = Path(__file__).resolve().parents[1]
        catalog = json.loads((root/'catalog.json').read_text())
        sku = 'hepiklim-forte-645-30'
        self.assertTrue(matches_name('Хепиклим форте 30 шт. капсулы массой 645 мг',catalog['products'][sku]))
        queries = json.loads((root/'research_config.json').read_text())['queries']
        self.assertEqual(sum(sku in x['targets'] for x in queries),2)
        self.assertFalse(any('лонгидаза' in str(p).lower() or 'суперлимф' in str(p).lower() for p in catalog['products'].values()))

    def test_public_urls_and_tracking(self):
        self.assertEqual(canonical('https://example.com/x?utm_source=a&b=2#top'),'https://example.com/x?b=2')
        for value in ['http://127.0.0.1/x','http://localhost/','http://169.254.169.254/','https://user:pass@example.com/','file:///etc/passwd']:
            with self.subTest(value=value), self.assertRaises(ValueError): canonical(value)
        with patch('internet_sources.socket.getaddrinfo',return_value=[(2,1,6,'',('10.0.0.1',443))]):
            with self.assertRaises(ValueError):public_url('https://example.com')

    def test_parser_extracts_evidence_and_rejects_unrelated(self):
        page=parse_page('https://example.com/news/test',HTML,self.config['all_brands'])
        self.assertEqual(page['brands'],['Хепиклим форте'])
        self.assertEqual(page['published'],'2025-05-01')
        self.assertEqual(len(page['questions']),1)
        self.assertTrue(page['advertisements'])
        self.assertEqual(page['products'][0]['amounts'],'645 мг')
        with self.assertRaises(ValueError):parse_page('https://example.com',HTML.replace('Хепиклим форте','Другой продукт'),self.config['all_brands'])

    def test_recommendations_and_generic_banners_are_not_brand_promos(self):
        raw=HTML.replace('</main>', '<p>Сокращает воспалительную реакцию</p><p>Зарегистрируйся и получи скидку на первый заказ</p><p>Скидка на Галавит</p></main>')
        page=parse_page('https://example.com/product/test',raw,self.config['all_brands'])
        self.assertEqual(page['brands'],['Хепиклим форте'])
        self.assertEqual(len(page['advertisements']),1)
        self.assertIn('Хепиклим',page['advertisements'][0])

    def test_collection_idempotence_failure_keeps_last_good_data(self):
        config=copy.deepcopy(self.config)
        config['sources']=[{'url':'https://example.com/news/test','kind':'page','brands':['Хепиклим форте']}]
        database=self.path/'web.sqlite3'
        with patch('internet_monitor.Fetcher.get',return_value=('https://example.com/news/test',HTML)):
            first=collect(config,database,no_ai=True,use_search=False)
            second=collect(config,database,no_ai=True,use_search=False)
        with patch('internet_monitor.Fetcher.get',side_effect=ValueError('HTTP 403')):
            third=collect(config,database,no_ai=True,use_search=False)
        for name in TABLES:
            for row in third['tables'][name]:self.assertEqual(len(row),len(TABLES[name]))
        self.assertEqual(len(second['tables']['Упоминания интернета']),1)
        self.assertEqual(first['tables']['Упоминания интернета'][0][8],second['tables']['Упоминания интернета'][0][8])
        self.assertEqual(len(third['tables']['Упоминания интернета']),1)
        self.assertTrue(any(row[2]=='Ошибка' for row in third['tables']['Источники интернета']))

    def test_duplicate_content_group(self):
        page=parse_page('https://example.com/a',HTML,self.config['all_brands'])
        group,fingerprint=duplicate_group(page,[])
        prior=dict(page,group_id=group,content_hash=fingerprint)
        self.assertEqual(duplicate_group(dict(page,url='https://other.com/b'),[prior])[0],group)

    def test_search_import_is_atomic_and_validated(self):
        path=self.path/'results.csv'
        fields=['brand','query','provider','region','observed_at','url','title','position','depth']
        row=['Хепиклим форте','Хепиклим форте','Test Search','Москва','2025-05-01T12:00:00+03:00','https://www.happyclim.ru/','Официальный сайт',1,10]
        with path.open('w',newline='') as f:
            w=csv.writer(f);w.writerow(fields);w.writerow(row);w.writerow(row)
        with open_database(self.path/'search.sqlite3') as db:
            with self.assertRaises(ValueError):import_search(db,path,self.config)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM web_search').fetchone()[0],0)
            with path.open('w',newline='') as f:
                w=csv.writer(f);w.writerow(fields);w.writerow(row)
            import_search(db,path,self.config)
            import_search(db,path,self.config)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM web_search').fetchone()[0],1)

    def test_openai_sources_do_not_invent_ranks(self):
        response={'output':[{'type':'web_search_call','action':{'sources':[{'url':'https://example.com/a','title':'A'}]}},
                            {'type':'message','content':[{'type':'output_text','annotations':[{'type':'url_citation','url':'https://example.com/a','title':'A'},{'type':'url_citation','url':'http://127.0.0.1/'}]}]}]}
        with patch('internet_search.api_response',return_value=response):
            found=openai_sources('test','secret','Москва','RU')
        self.assertEqual(len(found),1)
        self.assertNotIn('position',found[0])

    def test_ai_labels_require_verbatim_evidence_and_are_cached(self):
        page=parse_page('https://example.com/a',HTML,self.config['all_brands'])
        page['content_hash']='hash'
        label={'id':'0','topic':'Упаковка','audience':'Покупатели','kind':'Материал о продукте','message':'Сведения об упаковке','evidence':'Информация об упаковке'}
        def response(item):return {'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'items':[item]})}]}]}
        with open_database(self.path/'ai.sqlite3') as db:
            with patch('internet_search.api_response',return_value=response(dict(label,evidence='выдуманный фрагмент'))):
                self.assertEqual(classify_pages(db,[page],'secret'),{})
            with patch('internet_search.api_response',return_value=response(label)) as request:
                self.assertIn('hash',classify_pages(db,[page],'secret'))
                self.assertIn('hash',classify_pages(db,[page],'secret'))
                self.assertEqual(request.call_count,1)

    def test_summary_uses_aggregates(self):
        payload=json.loads(build_payload('report',['hepiklim-forte-645-30'],'Москва',{}, {'дата_среза':'2025-05-01','показатели':[]}))
        self.assertIn('интернет',payload)
        self.assertEqual(payload['область']['SKU'],['hepiklim-forte-645-30'])

if __name__=='__main__': unittest.main()
