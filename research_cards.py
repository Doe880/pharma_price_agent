import hashlib
import io
import json
import re
import warnings
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from lxml import etree, html
from PIL import Image, ImageOps

from research_visibility import clean
from product_utils import has_amount, has_form, has_brand


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def words(text):
    return set(re.findall(r"[a-zа-я0-9]+", clean(text)))


def plain(value):
    if not value:
        return ""
    try:
        raw = html.fragment_fromstring(str(value), create_parent=True)
        return " ".join(" ".join(raw.itertext()).split())
    except (ValueError, etree.ParserError):
        return " ".join(str(value).split())


def inspect_document(raw, card):
    document = html.fromstring(raw)
    items = []
    for node in document.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            value = json.loads(node)
        except ValueError:
            continue
        items.extend(value if isinstance(value, list) else [value])
    matches = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("@graph"), list):
            items_to_check = item["@graph"]
        else:
            items_to_check = [item]
        for value in items_to_check:
            if not isinstance(value, dict) or str(value.get("sku")) != card["id"]:
                continue
            types = value.get("@type", [])
            if types == "Product" or (isinstance(types, list) and "Product" in types):
                matches.append(value)
    if len(matches) != 1:
        raise ValueError("Структурированные данные нужного SKU отсутствуют")
    return document, matches[0]


def archived_zdravcity_reviews(day):
    archive = Path(__file__).resolve().parent / "example" / "zdravcity_reviews.json"
    sample = json.loads(archive.read_text(encoding="utf-8"))
    if day < date.fromisoformat(sample["collected_on"]):
        return []
    return sample["reviews"]


def get_reviews(document, item, card, url, day, limit):
    raw = item.get("review", [])
    raw = raw if isinstance(raw, list) else [raw]
    if card["source"] == "Здравсити":
        stored = document.xpath('//script[@id="__NEXT_DATA__"]/text()')
        if stored:
            try:
                product = json.loads(stored[0])["props"]["pageProps"]["productV2"]
            except (ValueError, KeyError, TypeError):
                product = None
            additional = product.get("reviews") if isinstance(product, dict) else None
            if isinstance(additional, list):
                raw = [*raw, *additional]
    prepared = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        body = plain(row.get("reviewBody") or row.get("message"))
        date_label = str(row.get("datePublished") or row.get("date") or "")
        try:
            when = (datetime.strptime(date_label[:10], "%d.%m.%Y").date()
                    if "." in date_label[:10] else date.fromisoformat(date_label[:10]))
        except ValueError:
            continue
        if not body or when > day:
            continue
        review_id = digest(json.dumps([card["source"], card["sku"], when.isoformat(), clean(body)],
                                      ensure_ascii=False))
        prepared[review_id] = {
            "id": review_id, "source": card["source"], "sku": card["sku"],
            "date": when.isoformat(), "body": body, "url": url,
        }
    return sorted(prepared.values(), key=lambda row: row["date"], reverse=True)[:limit]


def money(value):
    text = re.sub(r"[\s₽]", "", str(value or ""))
    if not re.fullmatch(r"\d{1,8}(?:[.,]\d{1,2})?", text):
        return None
    whole, _, part = text.replace(",", ".").partition(".")
    number = int(whole) * 100 + int(part.ljust(2, "0"))
    return number if 0 < number <= 100_000_000 else None


def url_for(card, value):
    url = urljoin(card["url"], str(value or ""))
    path = urlsplit(url)
    return url if path.scheme == "https" and path.hostname == urlsplit(card["url"]).hostname else ""


def offers_for(card, item):
    offers = item.get("offers", [])
    for offer in offers if isinstance(offers, list) else [offers]:
        if not isinstance(offer, dict) or not offer.get("url"):
            continue
        url = url_for(card, offer["url"])
        if url and urlsplit(url).path.rstrip("/") == urlsplit(card["url"]).path.rstrip("/"):
            yield offer


def promo_items(document, item, card):
    found = []
    offers = list(offers_for(card, item))
    if len(offers) == 1:
        offer = offers[0]
        specifications = offer.get("priceSpecification", [])
        if isinstance(specifications, dict):
            specifications = [specifications]
        for price in specifications:
            if not isinstance(price, dict):
                continue
            current, old = money(offer.get("price")), money(price.get("price"))
            if (str(price.get("priceType", "")).endswith("StrikethroughPrice")
                    and price.get("priceCurrency") == offer.get("priceCurrency") == "RUB"
                    and current and old and old > current
                    and str(offer.get("availability", "")).endswith("/InStock")):
                found.append(("Скидка на одну упаковку", card["url"], current, old,
                              "Зачёркнутая цена карточки", "discount", ""))
                break

    if card["source"] == "Apteka.ru":
        bundles = document.xpath('//*[contains(@class,"GoodsetsCarousel__stage")]//li//a[@href]')
        for bundle in bundles:
            title = plain(" ".join(bundle.xpath('./descendant::strong[1]/text()')))
            url = url_for(card, bundle.get("href"))
            prices = bundle.xpath('.//*[contains(@class,"GoodsetRecItem__price")]/*[contains(@class,"moneyprice")]')
            by_kind = {node.get("aria-invalid") == "true": money(node.text_content()) for node in prices}
            if title and url:
                found.append((title, url, by_kind.get(False), by_kind.get(True),
                              "Цена комплекта в блоке «Вместе выгоднее»", "bundle", ""))
    else:
        bundles = document.xpath(
            '//*[contains(@class,"Bundles_bundles-inner")]//*[contains(@class,"Card_card__")]'
        )
        for bundle in bundles:
            anchors = bundle.xpath('.//a[@href and @aria-label]')
            if not anchors:
                continue
            title = plain(anchors[0].get("aria-label"))
            url = url_for(card, anchors[0].get("href"))
            current = bundle.xpath('.//*[contains(@class,"content-action-price")]/text()')
            old = bundle.xpath('.//*[contains(@class,"common-price-currency-upper")]/text()')
            if title and url:
                found.append((title, url, money(current[0]) if current else None,
                              money(old[0]) if old else None, "Цена комплекта в блоке «Выгоднее»",
                              "bundle", ""))
    return [{"key": digest(card["source"] + "|" + urlsplit(url).path.rstrip("/") + "|" + kind),
             "source": card["source"], "sku": card["sku"], "title": title, "url": url,
             "current": current, "original": old if old and current and old > current else None,
             "terms": terms, "site_kind": kind, "ends": ends}
            for title, url, current, old, terms, kind, ends in found]


IMAGE_DOMAINS = {"images.apteka.ru", "static.zdravcity.ru"}


def primary_image(item):
    images = item.get("image", [])
    if not isinstance(images, list):
        images = [images]
    for image in images:
        url = image.get("url") if isinstance(image, dict) else image
        if url and urlsplit(url).scheme == "https" and urlsplit(url).netloc in IMAGE_DOMAINS:
            return url
    return ""


def fingerprint(url):
    request = Request(url, headers={"User-Agent": "PharmaPriceMonitor/0.2"})
    with urlopen(request, timeout=8) as response:
        if urlsplit(response.url).netloc not in IMAGE_DOMAINS:
            raise ValueError("Другой CDN")
        raw = response.read(5_000_001)
    if len(raw) > 5_000_000:
        raise ValueError("Фото больше 5 МБ")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as image:
            rgba = ImageOps.exif_transpose(image).convert("RGBA")
            base = Image.new("RGBA", rgba.size, "white")
            base.alpha_composite(rgba)
            gray = base.convert("L")
            bounds = ImageOps.invert(gray).point(lambda pixel: 255 if pixel > 25 else 0).getbbox()
            gray = gray.crop(bounds) if bounds else gray
            pix = list(gray.resize((9, 8), Image.Resampling.LANCZOS).getdata())
    return tuple(pix[y * 9 + x] > pix[y * 9 + x + 1] for y in range(8) for x in range(8))


def title_value(name, key):
    value = {"Дозировка": r"\d+(?:[.,]\d+)?\s*(?:мг|ме|ед|гр|г)(?![а-я])",
             "В упаковке": r"\d+\s*шт\b"}[key]
    match = re.search(value, clean(name))
    return match[0] if match else ""


def nominal_value(item, name, key):
    structured = item.get("additionalProperty") or []
    for prop in structured:
        if isinstance(prop, dict) and clean(prop.get("name")) == clean(key):
            return plain(prop.get("value"))
    return title_value(name, key)


def compact(value):
    return re.sub(r"[\s.]", "", clean(value)).replace("гр", "г")


def audit_cards(catalog, parsed, diagnostics, image_loader=fingerprint):
    rows = []
    for sku, spec in catalog["products"].items():
        cards = [card for card in catalog["cards"] if card["sku"] == sku]
        observed = []
        for card in cards:
            item = parsed.get(card["url"], (None, None))[1]
            if item is None:
                continue
            title = plain(item.get("name"))
            dose = nominal_value(item, title, "Дозировка")
            pack = nominal_value(item, title, "В упаковке")
            form = spec["form"]
            for field, result, expected, valid in [
                ("Дозировка", dose, spec["dose"], has_amount(dose, spec["dose"])),
                ("Размер упаковки", pack, str(spec["pack"]), compact(pack).removesuffix("шт") == str(spec["pack"])),
                ("Форма выпуска", title, form, has_form(title, spec)),
            ]:
                if not valid:
                    rows.append([spec["brand"], sku, "Высокий", field, card["source"],
                                 result or "Поле отсутствует", "", expected, card["url"], "",
                                 "Сверить с эталонным SKU и исправить карточку."])
            for key, observed_value in [("Дозировка", dose), ("В упаковке", pack)]:
                displayed = title_value(title, key)
                same_value = (has_amount(displayed, observed_value) if key == "Дозировка"
                              else compact(displayed).removesuffix("шт") == compact(observed_value).removesuffix("шт"))
                if displayed and observed_value and not same_value:
                    rows.append([spec["brand"], sku, "Высокий", key, card["source"],
                                 f"Название: {displayed}", "", f"Характеристики: {observed_value}",
                                 card["url"], "", "Согласовать название с характеристиками."])
            observed.append((card, item, title))

        for i, (card, item, title) in enumerate(observed):
            for other, alt, alt_title in observed[i + 1:]:
                brand = spec["brand"]
                def finding(priority, field, left, right, action, a=card["url"], b=other["url"]):
                    rows.append([brand, sku, priority, field, card["source"], left, other["source"],
                                 right, a, b, action])

                if compact(title) != compact(alt_title):
                    if has_brand(title, spec) and has_brand(alt_title, spec):
                        finding("Низкий", "Название", title, alt_title, "Сверить представление названия и порядок полей.")
                    else:
                        finding("Высокий", "Название", title, alt_title, "Проверить, соответствует ли название бренду.")
                description, alternative = (plain(value.get("description")) for value in (item, alt))
                first, second = words(description), words(alternative)
                score = len(first & second) / len(first | second) if first or second else 1
                if not first or not second or score < 0.55:
                    finding("Средний", "Описание", description[:95] or "Нет", alternative[:95] or "Нет",
                            f"Разное наполнение или структура, сходство слов {score:.0%}. Сверить по эталону.")
                left, right = primary_image(item), primary_image(alt)
                if not left or not right:
                    finding("Средний", "Фото", "Есть" if left else "Отсутствует", "Есть" if right else "Отсутствует",
                            "Проверить основное фото.", left or card["url"], right or other["url"])
                elif left != right:
                    try:
                        difference = sum(a != b for a, b in zip(image_loader(left), image_loader(right)))
                    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                        diagnostics.append(["Аудит", "Обе аптеки", sku, "Часть фото недоступна для сравнения", left])
                        continue
                    if difference >= 10:
                        finding("Средний", "Фото", "Основное изображение", "Основное изображение",
                                f"Визуальное отличие ({difference}/64). Проверить упаковку вручную.", left, right)
    priority = {"Высокий": 0, "Средний": 1, "Низкий": 2}
    return sorted(rows, key=lambda row: (priority[row[2]], row[0], row[3]))
