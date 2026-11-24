"""Optional search and public-social APIs; credentials never enter report rows."""
import json
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from internet_sources import CONTACTS, brand_mentions, canonical, iso_date


def get_json(url, label):
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=35) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("size")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("object expected")
        return value
    except HTTPError as error:
        raise ValueError(f"{label} — HTTP {error.code}") from None
    except (OSError, ValueError, UnicodeError):
        # URLs may contain API keys. Do not expose provider errors or request URLs.
        raise ValueError(f"{label} — ошибка сети или формата ответа") from None


def serp_results(query, key, engine, country, city, region_id=None, google_location=None):
    if engine not in {"google", "yandex"}:
        raise ValueError("Неизвестный поисковик")
    params = {"engine": engine, "api_key": key, "output": "json"}
    if engine == "google":
        params.update(q=query, hl="ru", gl=country.lower(), location=google_location or f"{city}, {country}", start=0)
    else:
        if len(query) > 400:
            raise ValueError("Запрос Яндекса длиннее 400 символов")
        params.update(text=query, lang="ru", yandex_domain="yandex.ru", p=0, groups_on_page=10)
        if region_id is not None:
            params["lr"] = region_id
    value = get_json("https://serpapi.com/search.json?" + urlencode(params), "SerpApi")
    if value.get("error") or value.get("search_metadata", {}).get("status") != "Success":
        raise ValueError("SerpApi не завершил поисковый запрос")
    raw = value.get("organic_results")
    # A successful empty search is different from a malformed/error response.
    if raw is None and value.get("search_information", {}).get("organic_results_state") == "Fully empty":
        raw = []
    if not isinstance(raw, list):
        raise ValueError("SerpApi не вернул органическую выдачу")
    ranked, seen_urls, seen_ranks = [], set(), set()
    for item in raw[:10]:
        if not isinstance(item, dict):
            continue
        rank = item.get("position")
        if type(rank) is not int or not 1 <= rank <= 100 or rank in seen_ranks:
            continue
        try:
            link = canonical(item.get("link"))
        except (TypeError, ValueError):
            continue
        if link in seen_urls:
            continue
        seen_urls.add(link)
        seen_ranks.add(rank)
        ranked.append({"url": link, "title": str(item.get("title", ""))[:350], "position": rank})
    depth = max([len(raw[:10]), *(item["position"] for item in ranked)])
    for item in ranked:
        item["depth"] = depth
    return ranked, depth


def threads_results(query, token, limit=20):
    params = {"q": query, "search_type": "RECENT", "search_mode": "KEYWORD", "limit": limit,
              "fields": "id,text,permalink,timestamp", "access_token": token}
    value = get_json("https://graph.threads.com/v1.0/keyword_search?" + urlencode(params), "Threads API")
    if value.get("error") or not isinstance(value.get("data"), list):
        raise ValueError("Threads API не вернул посты; проверьте токен и разрешение threads_keyword_search")
    rows, seen = [], set()
    for item in value["data"][:limit]:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        try:
            link = canonical(item.get("permalink"))
        except (TypeError, ValueError):
            continue
        host = urlsplit(link).hostname.removeprefix("www.")
        if host not in {"threads.com", "threads.net"} or "/post/" not in urlsplit(link).path or link in seen:
            continue
        seen.add(link)
        rows.append({"url": link, "text": item["text"][:40000], "published": iso_date(item.get("timestamp"))})
    return rows


def social_page(row, brands):
    body = CONTACTS.sub("[скрыто]", " ".join(row["text"].split()))
    names = brand_mentions(body, brands)
    if not names or len(body) < 5:
        raise ValueError("Упоминание бренда в посте Threads не подтверждено")
    questions = [part.strip() + "?" for part in body.split("?")[:-1]
                 if 10 <= len(part.strip()) <= 299][:6]
    return {"url": row["url"], "title": body[:180], "body": body, "brands": names,
            "published": row["published"], "questions": questions, "advertisements": [], "products": [],
            "kind": "Пост Threads", "origin": "Threads API"}


def source_category(url, channels):
    host = urlsplit(url).hostname or ""
    for channel in channels:
        if any(host == domain or host.endswith("." + domain) for domain in channel["domains"]):
            return channel["category"], channel["name"]
    return "Другие источники", host
