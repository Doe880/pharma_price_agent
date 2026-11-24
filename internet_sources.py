import hashlib
import ipaddress
import json
import re
import socket
import time
from datetime import date, datetime, timezone, timedelta
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from lxml import etree, html

USER_AGENT = "PharmaInternetMonitor/1.0"
TRACKING = {"gclid", "fbclid", "yclid", "ysclid", "_openstat", "ref"}
FORM = re.compile(r"капсул\w*|суппозитори\w*|таблет\w*|саше|порош\w*|флакон\w*|раствор\w*", re.I)
SIZE = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:мг|мкг|мл|г|ме|ед)(?![а-я])", re.I)
PACK = re.compile(r"(?:№|N)\s*\d+|\b\d+\s*(?:шт\.?|капсул\w*|саше|флакон\w*)", re.I)
CONTACTS = re.compile(r'\b[\w.+-]+@[\w.-]+\.\w+\b|(?:\+7|(?<!\d)8)[\d ()-]{9,}\d')


def normalize(value):
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def canonical(url):
    parts = urlsplit(str(url).strip())
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Нужна публичная HTTP(S)-ссылка")
    if parts.port not in {None, 80, 443}:
        raise ValueError("Необычный порт")
    host = parts.hostname.lower().rstrip(".").encode("idna").decode("ascii")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
        raise ValueError("Локальный адрес")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Непубличный адрес")
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in TRACKING]
    return urlunsplit((parts.scheme, host, parts.path or "/", urlencode(sorted(query)), ""))


def public_url(url):
    value = canonical(url)
    parts = urlsplit(value)
    addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Адрес сайта относится к непубличной сети")
    return value


class CheckedRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        return super().redirect_request(request, fp, code, message, headers, public_url(url))


class Fetcher:
    def __init__(self, timeout=12, delay=1.2):
        self.timeout = timeout
        self.delay = delay
        self.robots = {}
        self.last_request = {}
        self.opener = build_opener(CheckedRedirect())

    def raw(self, url):
        url = public_url(url)
        host = urlsplit(url).hostname
        pause = self.delay - (time.monotonic() - self.last_request.get(host, 0))
        if pause > 0:
            time.sleep(pause)
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"})
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(4_000_001)
                charset = response.headers.get_content_charset() or "utf-8"
                final_url = canonical(response.url)
            if len(body) > 4_000_000:
                raise ValueError("Ответ больше 4 МБ")
            return final_url, body.decode(charset, errors="replace")
        finally:
            self.last_request[host] = time.monotonic()

    def get(self, url):
        url = canonical(url)
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            try:
                _, text = self.raw(origin + "/robots.txt")
                if text.lstrip().startswith("<"):
                    raise ValueError("Вместо robots.txt получен HTML")
                rules = RobotFileParser()
                rules.parse(text.splitlines())
                self.robots[origin] = rules
            except HTTPError as error:
                if error.code not in {404, 410}:
                    raise ValueError(f"robots.txt недоступен — HTTP {error.code}") from None
                self.robots[origin] = None
        rules = self.robots[origin]
        if rules is not None and not rules.can_fetch(USER_AGENT, url):
            raise ValueError("Страница закрыта для обхода в robots.txt")
        return self.raw(url)


def brand_mentions(text, brands):
    value = normalize(text)
    return [brand["name"] for brand in brands if any(
        re.search(r"(?<!\w)" + re.escape(normalize(alias)) + r"(?!\w)", value)
        for alias in brand["aliases"]
    )]


def iso_date(value):
    if not value:
        return None
    value = str(value).strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            parsed = datetime.strptime(value[:10], "%d.%m.%Y").date()
        except ValueError:
            return None
    return parsed.isoformat() if date(1990, 1, 1) <= parsed <= datetime.now(timezone(timedelta(hours=3))).date() else None


def text_of(element):
    return CONTACTS.sub("[скрыто]", " ".join(" ".join(element.itertext()).split()))


def json_nodes(document):
    result = []
    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            result.append(value)
            for field in ("@graph", "mainEntity", "hasPart", "itemListElement", "item"):
                if field in value:
                    visit(value[field])
    for raw in document.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            visit(json.loads(raw))
        except ValueError:
            continue
    return result


def parse_page(url, raw, brands):
    document = html.fromstring(raw)
    nodes = json_nodes(document)
    titles = document.xpath('//h1//text()') or document.xpath('//title/text()')
    title = " ".join(" ".join(titles).split())[:350]
    dates = document.xpath(
        '//meta[@property="article:published_time"]/@content | //meta[@name="date"]/@content | //time/@datetime'
    )
    dates.extend(node.get("datePublished") for node in nodes if node.get("datePublished"))
    published = next((iso_date(value) for value in dates if iso_date(value)), None)
    for element in document.xpath('//script|//style|//noscript|//nav|//footer|//header|//form'):
        element.drop_tree()
    mains = document.xpath('//main|//article|//*[@role="main"]')
    main = max(mains, key=lambda node: len(node.text_content())) if mains else document
    body = text_of(main)[:40000]
    if len(body) < 70 or any(marker in normalize(title + " " + body[:400]) for marker in (
        "just a moment", "доступ ограничен", "подтвердите, что вы", "проверка браузера", "access denied"
    )):
        raise ValueError("Получена блокировка или пустая страница")
    names = brand_mentions(title + " " + body, brands)
    if not names:
        raise ValueError("Упоминание выбранных брендов на странице не подтверждено")
    if (urlsplit(url).hostname.removeprefix("www.") in {"threads.com", "threads.net"}
            and not brand_mentions(body, brands)):
        raise ValueError("Threads: текст поста недоступен; ссылка остаётся в поисковых результатах")
    focus = brand_mentions(title, brands)
    if focus:
        names = focus
    host = urlsplit(url).hostname.removeprefix("www.")
    official_names = [b["name"] for b in brands if any(
        host == domain.removeprefix("www.") or host.endswith("." + domain.removeprefix("www."))
        for domain in b.get("domains", [])
    )]
    questions = []
    for node in nodes:
        if "Question" in ([node.get("@type")] if isinstance(node.get("@type"), str) else node.get("@type", [])):
            question = str(node.get("name", "")).strip()
            if 10 <= len(question) <= 300 and (brand_mentions(question, brands) or len(official_names) == 1):
                questions.append(question)
    for node in main.xpath('.//h1|.//h2|.//h3|.//h4|.//summary|.//*[@itemprop="name"]|.//*[contains(@class,"faq")]//p'):
        value = text_of(node)
        faq = any("faq" in normalize(ancestor.get("class", "") + " " + ancestor.get("id", ""))
                  for ancestor in [node, *node.iterancestors()])
        if (value.endswith("?") and 10 <= len(value) <= 300
                and (brand_mentions(value, brands) or (faq and len(official_names) == 1))):
            questions.append(value)
    questions = list(dict.fromkeys(questions))[:6]

    advertisements = []
    markers = re.compile(r"(?<!\w)(?:скидк\w*|промокод\w*|акци[яиюй]|в подарок|выгоднее|реклама|erid)(?!\w)", re.I)
    for node in main.xpath('.//h1|.//h2|.//h3|.//p|.//li|.//img[@alt]'):
        value = node.get("alt", "") if node.tag == "img" else text_of(node)
        if (15 <= len(value) <= 250 and markers.search(normalize(value))
                and (set(brand_mentions(value, brands)) & set(names))):
            advertisements.append(value)
    advertisements = list(dict.fromkeys(advertisements))[:4]

    products = []
    for node in nodes:
        types = node.get("@type", [])
        if "Product" not in ([types] if isinstance(types, str) else types):
            continue
        name = str(node.get("name", "")).strip()
        if name and brand_mentions(name, brands):
            products.append({"name": name[:300], "url": url, "kind": "Product JSON-LD"})
    for element in main.xpath('.//h1|.//h2|.//h3|.//a[@href]'):
        value = text_of(element)
        if 12 <= len(value) <= 220 and FORM.search(value) and brand_mentions(value, brands):
            try:
                link = canonical(urljoin(url, element.get("href"))) if element.get("href") else url
            except ValueError:
                continue
            products.append({"name": value, "url": link, "kind": "Название на странице"})
    unique_products = {}
    for row in products:
        if set(brand_mentions(row["name"], brands)) & set(names):
            unique_products.setdefault((row["name"], row["url"]), row)
    products = list(unique_products.values())[:12]
    for product in products:
        product["forms"] = ", ".join(dict.fromkeys(match.group().lower() for match in FORM.finditer(product["name"])))
        product["amounts"] = ", ".join(dict.fromkeys(match.group() for match in SIZE.finditer(product["name"])))
        product["pack"] = ", ".join(dict.fromkeys(match.group() for match in PACK.finditer(product["name"])))

    category = "Материал о продукте"
    if "/product/" in url or "/p_" in url:
        category = "Карточка товара"
    elif questions and not published:
        category = "Вопросы и ответы"
    elif published or any(token in url for token in ("/article", "/news", "/blog", "/content/")):
        category = "Публикация"
    return {"url": url, "title": title or names[0], "body": body, "brands": names,
            "published": published, "questions": questions, "advertisements": advertisements,
            "products": products, "kind": category}


def index_links(url, raw, brands, limit):
    document = html.fromstring(raw)
    origin = urlsplit(url).hostname.removeprefix("www.")
    found = []
    for anchor in document.xpath('//a[@href]'):
        try:
            link = canonical(urljoin(url, anchor.get("href")))
        except (ValueError, UnicodeError):
            continue
        if urlsplit(link).hostname.removeprefix("www.") != origin:
            continue
        if re.search(r"\.(pdf|jpe?g|png|webp|zip|mp4|docx?)$", urlsplit(link).path, re.I):
            continue
        value = anchor.text_content() + " " + link
        if brand_mentions(value, brands) or any(token in link.casefold() for token in (
            "article", "news", "faq", "question", "product", "instruct", "for-men", "for-women", "dlja", "catalog"
        )):
            if link not in found and link != canonical(url):
                found.append(link)
    return found[:limit]


def feed_links(url, raw, limit):
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(raw.encode("utf-8"), parser)
    found = root.xpath(
        '//*[local-name()="item"]/*[local-name()="link"]/text() | '
        '//*[local-name()="entry"]/*[local-name()="link"]/@href | '
        '//*[local-name()="url"]/*[local-name()="loc"]/text()'
    )
    result = []
    for link in found:
        try:
            value = canonical(urljoin(url, link))
        except ValueError:
            continue
        if value not in result:
            result.append(value)
    return result[:limit]


def duplicate_group(page, prior):
    text = normalize(page["body"])
    exact = hashlib.sha256(text.encode()).hexdigest()
    tokens = text.split()
    shingles = {" ".join(tokens[i:i + 5]) for i in range(max(0, len(tokens) - 4))}
    for row in prior:
        if page["url"] == row["url"]:
            return row["group_id"], exact
        if exact == row["content_hash"]:
            return row["group_id"], exact
        old = normalize(row["body"]).split()
        if len(tokens) >= 80 and len(old) >= 80 and set(page["brands"]) & set(row["brands"]):
            old_shingles = {" ".join(old[i:i + 5]) for i in range(len(old) - 4)}
            union = shingles | old_shingles
            if union and len(shingles & old_shingles) / len(union) >= 0.85:
                return row["group_id"], exact
    return hashlib.sha256(page["url"].encode()).hexdigest(), exact
