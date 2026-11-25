import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent import parse_card
from product_utils import has_amount, matches_name
from research_cards import audit_cards


class HepiklimTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]
        self.catalog=json.loads((root/'catalog.json').read_text())
        self.sku='hepiklim-forte-645-30'
        self.spec=self.catalog['products'][self.sku]
        self.card=next(c for c in self.catalog['cards'] if c['sku']==self.sku and c['source']=='Здравсити')

    def test_equivalent_mass_and_identity(self):
        for name in ['Хепиклим Форте капсулы 0,645г 30шт','Хепиклим Форте капсулы 0.645 г 30 шт.',
                     'Хепиклим Форте капсулы 645 мг 30шт']:
            self.assertTrue(matches_name(name,self.spec),name)
        for name in ['Хепиклим Форте капсулы 645г 30шт','Хепиклим Форте капсулы 0,0645г 30шт',
                     'Хепиклим Форте капсулы 0,645г 60шт','2Х Хепиклим Форте капсулы 0,645г 30шт',
                     'Хепиклим Форте таблетки 0,645г 30шт']:
            self.assertFalse(matches_name(name,self.spec),name)
        self.assertTrue(has_amount('0,1 г','100 мг'))
        self.assertFalse(has_amount('1100 мг','100 мг'))
        self.assertFalse(has_amount('0.645 мг','645 мг'))

    def test_zdravcity_card_and_public_offer(self):
        name='Хепиклим Форте капсулы 0,645г 30шт'
        item={'@type':'Product','sku':'356335','name':name,'offers':{'@type':'Offer',
              'url':self.card['url'],'price':1234.56,'priceCurrency':'RUB','availability':'https://schema.org/InStock'}}
        raw='<html><title>'+name+' купить в Москве</title><script type="application/ld+json">'+json.dumps(item)+'</script></html>'
        self.assertEqual(parse_card(self.card,self.spec,'Москва',raw),(123456,'in_stock'))
        item['name']='2Х '+name
        bad='<html><title>Купить в Москве</title><script type="application/ld+json">'+json.dumps(item)+'</script></html>'
        with self.assertRaises(ValueError):parse_card(self.card,self.spec,'Москва',bad)

    def test_audit_does_not_flag_equivalent_mass_units(self):
        item={'name':'Хепиклим Форте капсулы 0,645г 30шт',
              'additionalProperty':[{'name':'Дозировка','value':'645 мг'}]}
        rows=audit_cards({'products':{self.sku:self.spec},'cards':[self.card]},
                         {self.card['url']:(None,item)},[],image_loader=lambda url:())
        self.assertFalse(any('Дозировка' in row[3] for row in rows),rows)
        item['additionalProperty'][0]['value']='64,5 мг'
        rows=audit_cards({'products':{self.sku:self.spec},'cards':[self.card]},
                         {self.card['url']:(None,item)},[],image_loader=lambda url:())
        self.assertTrue(any('Дозировка' in row[3] for row in rows),rows)


if __name__=='__main__':unittest.main()
