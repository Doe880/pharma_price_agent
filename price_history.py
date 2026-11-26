"""Look up earlier observations for Zdravcity cards denied in a selected run."""
import pandas as pd


def last_zdravcity_checks(failed, history, reference):
    keys = ["Площадка", "Продавец", "Город", "Карточка", "Тип цены"]
    columns = ["Товар", "Город", "Цена за уп.", "Наличие", "Время МСК", "Канал", "Источник данных", "Карточка"]
    attempts = failed.loc[
        failed["Наличие"].eq("Ошибка проверки") & failed["Канал"].eq("Сайт"),
        keys + ["Товар", "Время МСК"],
    ].rename(columns={"Время МСК": "Время отказа"})
    sources = pd.concat([
        reference.assign(**{"Источник данных": "Контрольный сбор 26.11.2026"}),
        history.assign(**{"Источник данных": "История выбранного отчёта"}),
    ], ignore_index=True)
    sources["Время МСК"] = pd.to_datetime(sources["Время МСК"], errors="coerce")
    matches = attempts.merge(sources[keys + ["Канал", "Наличие", "Время МСК", "Цена за уп.", "Источник данных"]],
                             on=keys, how="inner")
    valid = matches[
        matches["Канал"].isin(["Сайт", "Выгрузка Здравсити"])
        & matches["Наличие"].isin(["Доступен к заказу", "Нет в наличии"])
        & matches["Время МСК"].le(matches["Время отказа"])
    ]
    latest = valid.sort_values("Время МСК", kind="stable").drop_duplicates(keys, keep="last")
    return latest[columns].sort_values("Товар").reset_index(drop=True)
