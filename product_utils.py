import re
from decimal import Decimal


def normalized(value):
    return " ".join(str(value).casefold().replace("ё", "е").split())


def has_amount(name, amount):
    value = re.escape(normalized(amount)).replace(r"\ ", r"\s*")
    if re.search(rf"(?<![\d.,]){value}(?![\dа-яa-z])", normalized(name)):
        return True
    # Retailers may describe the same capsule as 645 mg or 0.645 g.
    # Decimal conversion keeps the identity check exact rather than using rounded floats.
    target = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(мкг|мг|г)", normalized(amount))
    if not target:
        return False
    factors = {"г": Decimal(1), "мг": Decimal("0.001"), "мкг": Decimal("0.000001")}
    grams = Decimal(target[1].replace(",", ".")) * factors[target[2]]
    matches = re.finditer(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(мкг|мг|г)(?!\w)", normalized(name))
    return any(Decimal(match[1].replace(",", ".")) * factors[match[2]] == grams for match in matches)


def has_brand(name, product):
    name = normalized(name)
    aliases = product.get("name_prefixes", [product["brand"]])
    return any(re.match(re.escape(normalized(alias)) + r"(?:\s|$)", name) for alias in aliases)


def has_form(name, product):
    name = normalized(name)
    return all(term in name for term in product.get("form_terms", product["form"].split()))


def matches_name(name, product):
    return (has_brand(name, product) and has_amount(name, product["dose"])
            and bool(re.search(rf"(?<!\d){product['pack']}\s*шт\b", normalized(name)))
            and has_form(name, product))


def product_label(product):
    return f"{product['brand']} · {product['form']} · {product['dose']} · {product['pack']} шт."
