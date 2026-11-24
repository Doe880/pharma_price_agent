import re


def normalized(value):
    return " ".join(str(value).casefold().replace("ё", "е").split())


def has_amount(name, amount):
    value = re.escape(normalized(amount)).replace(r"\ ", r"\s*")
    return bool(re.search(rf"(?<![\d.,]){value}(?![\dа-яa-z])", normalized(name)))


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
