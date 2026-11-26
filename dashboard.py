import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import BadZipFile

import altair as alt
import pandas as pd
import streamlit as st
from openpyxl import load_workbook
from dotenv import load_dotenv

from dashboard_collection import partner_template, run_collection
from price_history import last_zdravcity_checks
from product_utils import product_label
from internet_dashboard import render as render_internet, load_report as load_internet_report
from internet_sources import brand_mentions
from internet_monitor import load_config as load_internet_config, TABLES as INTERNET_TABLES

from dashboard_summary import build_payload, cache_key, cached_summary, generate_summary, save_summary

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)
PALETTE = ["#125965", "#DF8B55", "#466DA1", "#8C729E", "#7B8F45", "#C05560"]
DATA_DIR = Path(os.getenv("PHARMA_DATA_DIR", ROOT / "data")).expanduser().resolve()
EXAMPLE_DIR = ROOT / "example"
CATALOG = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
PRODUCTS = CATALOG["products"]
NAMES = {sku: product_label(product) for sku, product in PRODUCTS.items()}

st.set_page_config(page_title="Мониторинг аптечных витрин", layout="wide")
st.markdown("""
<style>
.stApp {background: #f7f8f5; color: #172e33;}
[data-testid="stSidebar"] {background: #eef3ef;}
.block-container {padding-top: 2.25rem; max-width: 1390px;}
h1, h2, h3 {color: #173c43; letter-spacing: -.025em;}
[data-testid="stMetric"] {
  background: #fff; border: 1px solid #dbe7e2; border-top: 3px solid #3f8b85;
  border-radius: 10px; padding: .9rem 1rem;
}
[data-testid="stMetricValue"] {color: #125965; font-weight: 650;}
[data-baseweb="tab-list"] {gap: .35rem;}
[data-testid="stVegaLiteChart"] {border: 1px solid #e2e9e4; border-radius: 10px; padding: .45rem;}
</style>
""", unsafe_allow_html=True)


@st.cache_data(show_spinner=False)
def read_report(path, modified, size):
    # File metadata is part of the cache key so a replaced report is reloaded.
    workbook = load_workbook(path, data_only=True)
    frames = {}
    try:
        heading = workbook["Сводка"]["A1"].value if "Сводка" in workbook else Path(path).stem
        for sheet in workbook:
            if sheet.title == "Сводка":
                continue
            header, *rows = sheet.iter_rows()
            names = [cell.value for cell in header]
            result = []
            for row in rows:
                values = []
                for cell in row:
                    link = cell.hyperlink.target if cell.hyperlink else None
                    values.append(link if link and urlsplit(link).scheme == "https" else cell.value)
                if any(value is not None for value in values):
                    result.append(values)
            frames[sheet.title] = pd.DataFrame(result, columns=names)
    finally:
        workbook.close()
    return heading, frames


def data(sheet):
    return frames.get(sheet, pd.DataFrame()).copy()


def filters(table, geo=True):
    if "SKU" in table:
        table = table[table["SKU"].isin(selected_skus)].assign(**{"Товар": lambda t: t["SKU"].map(NAMES)})
    elif "Препарат" in table and {"Дозировка", "Шт."} <= set(table):
        product_labels = {
            (product["brand"], product["dose"], product["pack"]): NAMES[sku]
            for sku, product in PRODUCTS.items() if sku in selected_skus
        }
        keys = zip(table["Препарат"], table["Дозировка"], pd.to_numeric(table["Шт."], errors="coerce"))
        table = table.assign(Товар=[product_labels.get(key) for key in keys])
        table = table[table["Товар"].notna()]
    if geo and "Город" in table and city != "Все города":
        table = table[table["Город"] == city]
    return table


def table(frame, message="Данных для выбранных фильтров пока нет.", row_height=35):
    if frame.empty:
        st.info(message)
        return
    groups = {
        "link": ("Источник", "Источник 1", "Источник 2", "Источник A", "Источник Б", "Карточка", "Выдача"),
        "rate": ("Доля", "Доля топ‑10", "Скидка", "Макс. скидка"),
        "money": ("Цена за уп.", "Цена за шт.", "Цена", "Зачёркнутая цена", "Цена вчера", "Цена сейчас"),
        "points": ("Изменение, п.п.", "Изменение, %"),
    }
    frame = frame.copy()
    config = {}
    for column in groups["link"]:
        if column in frame:
            config[column] = st.column_config.LinkColumn(column, display_text="Открыть")
    for column in groups["rate"]:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").map(
                lambda value: f"{value:.1%}" if pd.notna(value) else None
            )
    for column in groups["money"]:
        if column in frame:
            config[column] = st.column_config.NumberColumn(column, format="%.2f ₽")
    for column in groups["points"]:
        if column in frame:
            config[column] = st.column_config.NumberColumn(column, format="%.1f")
    if "Текст отзыва" in frame:
        config["Текст отзыва"] = st.column_config.TextColumn("Текст отзыва", width="large")
    st.dataframe(frame, hide_index=True, width="stretch", column_config=config, row_height=row_height)


def chart(spec, height=290):
    themed = spec.properties(height=height).configure_view(strokeWidth=0).configure_axis(
        labelFontSize=12, titleFontSize=12, gridColor="#e4ece8", domainColor="#c6d7d2"
    ).configure_legend(labelFontSize=12, titleFontSize=12)
    st.altair_chart(themed, width="stretch")


def grouped_bars(frame, category, value, series, axis_format=None, domain=None):
    values = frame[[category, value, series]].copy()
    values.columns = ["category", "amount", "series"]
    title = "Цена за упаковку, ₽" if value == "Цена за уп." else value
    return alt.Chart(values).mark_bar(cornerRadiusEnd=3).encode(
        x=alt.X("category:N", title=None, axis=alt.Axis(labelAngle=-15, labelLimit=200)),
        y=alt.Y("amount:Q", title=title, axis=alt.Axis(format=axis_format),
                scale=alt.Scale(domain=domain) if domain else alt.Scale(zero=True)),
        color=alt.Color("series:N", scale=alt.Scale(range=PALETTE), title=series),
        xOffset="series:N",
        tooltip=[alt.Tooltip("category:N", title=category), alt.Tooltip("series:N", title=series),
                 alt.Tooltip("amount:Q", title=title, format=axis_format)],
    )


def ensure_prices(frame):
    if frame.empty:
        return frame
    frame["Цена за уп."] = pd.to_numeric(frame["Цена за уп."], errors="coerce")
    frame["Время МСК"] = pd.to_datetime(frame["Время МСК"], errors="coerce")
    return frame


def direct_prices(frame):
    if frame.empty:
        return frame
    return frame[
        (frame["Тип цены"] == "Публичная") & (frame["Наличие"] == "Доступен к заказу")
        & frame["Цена за уп."].notna() & frame["Канал"].isin(["Сайт", "Выгрузка Здравсити"])
    ].copy()


st.title("Мониторинг аптечных витрин")
with st.sidebar:
    st.header("Обновление данных")
    with st.expander("OpenAI для сводки"):
        api_key = st.text_input("API-ключ", type="password",
                                help="Можно задать OPENAI_API_KEY в .env или Secrets Streamlit.")
        api_key = api_key.strip() or os.getenv("OPENAI_API_KEY", "").strip()
        st.caption("Модель gpt-6-luna. Запрос выполняется по кнопке.")
    include_internet = st.checkbox("Включить интернет-мониторинг", value=True)
    api_available = bool(api_key or any(os.getenv(name) for name in
                         ("BRAVE_SEARCH_API_KEY", "SERPAPI_API_KEY", "THREADS_ACCESS_TOKEN")))
    use_api = st.checkbox("Использовать подключённые API (ИИ и поиск)", value=False, disabled=not api_available,
                          help="Включает ИИ-разметку и доступные поисковые API. Запросы могут оплачиваться по тарифам провайдеров.")
    with st.expander("Выгрузка Здравсити"):
        st.caption("Сначала проверяем сайт. Для недоступных карточек можно загрузить публичные цены и наличие "
                   "по Москве из согласованной выгрузки за сегодня. Отзывы и фотографии требуют доступа к карточке.")
        st.download_button("Скачать шаблон CSV", partner_template(CATALOG), "zdravcity_template.csv", "text/csv")
        st.caption("Оставьте нужные товары. Укажите observed_at с часовым поясом, availability (in_stock, "
                   "out_of_stock или unknown) и цену упаковки price_rub. Для out_of_stock оставьте цену пустой.")
        feed = st.file_uploader("Согласованная выгрузка за сегодня", type=["csv"])
    st.caption("Сбор выполняется для всего каталога и может занять несколько минут.")
    if st.button("Собрать свежие данные", type="primary"):
        try:
            with st.spinner("Собираю цены, отзывы, промо и материалы…"):
                collected = run_collection(DATA_DIR, api_key=api_key, use_api=use_api,
                                           include_internet=include_internet,
                                           partner_feed=feed.getvalue() if feed is not None else None)
            st.session_state["pending_report"] = collected["path"]
            st.session_state["collection_result"] = collected
            st.rerun()
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
            st.error(str(error))

collection_result = st.session_state.pop("collection_result", None)
if collection_result:
    if collection_result["partial"]:
        st.warning("Новый отчёт готов. Часть аптечных карточек не загрузилась — смотрите «Диагностика» и таблицу проверок.")
    else:
        st.success("Новый отчёт готов и открыт. Проверки отдельных интернет-источников доступны во вкладке «Интернет».")
reports = sorted(DATA_DIR.glob("prices_*.xlsx"), key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
examples = sorted(EXAMPLE_DIR.glob("*.xlsx"), reverse=True)
choices = reports + examples
if not choices:
    st.info("Нажмите «Собрать свежие данные» в боковой панели, чтобы создать первый отчёт.")
    st.stop()

if st.session_state.get("selected_report") not in choices:
    st.session_state.pop("selected_report", None)
pending_report = st.session_state.pop("pending_report", None)
if pending_report in choices:
    st.session_state["selected_report"] = pending_report

with st.sidebar:
    st.header("Выборка")
    report = st.selectbox("Отчёт", choices, format_func=lambda p: (
        ("Пример · " if p.parent == EXAMPLE_DIR else "") + p.name
    ), key="selected_report")
    st.button("Проверить новые отчёты", help="Обновляет список готовых Excel-отчётов.")
    try:
        stat = report.stat()
        heading, frames = read_report(str(report), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError, KeyError, TypeError, BadZipFile) as exc:
        st.error(f"Не удалось открыть отчёт: {exc}")
        st.stop()
    cities = sorted({value for frame in frames.values() if "Город" in frame
                     for value in frame["Город"].dropna().unique() if isinstance(value, str) and value})
    city = st.selectbox("Город", ["Все города", *cities], index=1 if len(cities) == 1 else 0)
    selected_skus = st.multiselect("Товары", list(NAMES), default=list(NAMES), format_func=NAMES.__getitem__)
    with report.open("rb") as stream:
        st.download_button("Скачать Excel", stream.read(), file_name=report.name)

st.caption(heading)
if report.parent == EXAMPLE_DIR:
    st.info("Демонстрационный срез. Нажмите «Собрать свежие данные» в боковой панели, чтобы открыть свои данные.")
if not selected_skus:
    st.info("Выберите хотя бы один товар в боковой панели.")
    st.stop()

summary_tab, price_tab, review_tab, visibility_tab, audit_tab, promo_tab, internet_tab, check_tab = st.tabs(
    ["ИИ-сводка", "Цены", "Отзывы", "Видимость", "Аудит карточек", "Промо", "Интернет", "Диагностика"]
)

with summary_tab:
    st.subheader("Выводы по выбранному отчёту")
    st.caption("Luna получает рассчитанные показатели с учётом фильтров. Запрос к API платный. "
               "Готовая сводка сохраняется для этой выборки.")
    scoped = {name: filters(frame, geo=name not in {"Отзывы по темам", "Аудит карточек"})
              for name, frame in frames.items()
              if name in {"Проверки", "Отзывы по темам", "Видимость", "Аудит карточек",
                          "Календарь промо", "Частота промо"}}
    selected_brands = brand_mentions(" ".join(PRODUCTS[sku]["brand"] for sku in selected_skus),
                                     load_internet_config()["all_brands"])
    internet_aggregates = None
    try:
        internet_report, internet_example = load_internet_report(DATA_DIR)
        if internet_report:
            internet_aggregates = {
                "дата_среза": internet_report["generated_at"], "пример": internet_example,
                "показатели": [dict(zip(INTERNET_TABLES["Интернет конкурентов"], row))
                               for row in internet_report["tables"]["Интернет конкурентов"]
                               if row[0] in selected_brands],
                "методика": internet_report.get("methodology", []),
            }
    except (OSError, ValueError, KeyError):
        pass
    # Keep the four-argument call compatible with modules cached during a Streamlit update.
    payload = build_payload(heading, selected_skus, city, scoped)
    if internet_aggregates is not None:
        payload_data = json.loads(payload)
        payload_data["интернет"] = internet_aggregates
        payload = json.dumps(payload_data, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    payload_key = cache_key(payload)
    summary_path = DATA_DIR / "dashboard_summaries.sqlite3"
    summary = st.session_state.get("summary_results", {}).get(payload_key)
    if summary is None:
        try:
            summary = cached_summary(summary_path, payload)
        except (OSError, sqlite3.Error):
            st.warning("Не удалось прочитать сохранённые ИИ-сводки.")
    has_data = any(not frame.empty for frame in scoped.values())
    if not api_key:
        st.info("Для новой сводки задайте OPENAI_API_KEY в .env или введите API-ключ в боковой панели.")
    if not has_data:
        st.info("Для выбранной выборки нет показателей.")
    if st.button("Сформировать ИИ-сводку", disabled=not api_key or not has_data or summary is not None):
        try:
            with st.spinner("Luna анализирует показатели…"):
                summary = generate_summary(payload, api_key)
            st.session_state.setdefault("summary_results", {})[payload_key] = summary
            try:
                save_summary(summary_path, payload, summary)
            except (OSError, sqlite3.Error):
                st.warning("Сводка показана в этом окне. Запись на диск не удалась.")
            else:
                st.rerun()
        except ValueError as exc:
            st.error(str(exc))
        except (KeyError, TypeError):
            st.error("Не удалось сформировать сводку. Проверьте ключ, баланс и соединение с OpenAI.")
    if summary:
        created_at = datetime.fromisoformat(summary["created_at"]).astimezone(timezone(timedelta(hours=3)))
        st.caption("Сформировано " + created_at.strftime("%d.%m.%Y %H:%M") + " МСК · gpt-6-luna")
        st.markdown(summary["text"])
        st.download_button("Скачать сводку", summary["text"].encode("utf-8"),
                           file_name="pharma_ai_summary.md", mime="text/markdown")
        st.caption("Сверяйте выводы с графиками и первоисточниками.")


with price_tab:
    current = ensure_prices(filters(data("Проверки")))
    history = ensure_prices(filters(data("История")))
    if {"Площадка", "Диагностика"} <= set(current):
        denied = (current["Площадка"] == "Здравсити") & current["Диагностика"].fillna("").str.startswith("HTTP 403")
        if denied.any():
            st.warning(f"Здравсити отклонил запросы по {int(denied.sum())} карточкам. "
                       "Для получения цен за сегодня можно загрузить согласованную выгрузку в боковой панели.")
            reference = pd.read_csv(EXAMPLE_DIR / "zdravcity_2026-11-26.csv")
            previous = last_zdravcity_checks(current.loc[denied], history, reference)
            if not previous.empty:
                st.subheader("Последняя успешная проверка Здравсити до отказа")
                st.caption("Цена и наличие указаны на время наблюдения по Москве. "
                           "Текущие показатели рассчитываются отдельно.")
                table(previous)
    facts = direct_prices(current)
    historic = direct_prices(history)
    measures = st.columns(3)
    measures[0].metric("Карточек проверено", len(current))
    measures[1].metric("Публичных цен в наличии", len(facts))
    measures[2].metric("Аптек с публичной ценой", facts["Площадка"].nunique() if len(facts) else 0)
    st.subheader("Цена за упаковку")
    if len(facts):
        frame = facts.assign(**{"Витрина": facts["Площадка"] + " · " + facts["Город"]}) if city == "Все города" else facts
        series = "Витрина" if city == "Все города" else "Площадка"
        chart(grouped_bars(frame, "Товар", "Цена за уп.", series, ".2f"))
    else:
        st.info("В этой выборке нет подтверждённых публичных цен с наличием.")
    st.subheader("История цен")
    if not historic.empty:
        historic = historic.dropna(subset=["Время МСК"])
        historic["Серия"] = historic["Площадка"] + " · " + historic["Товар"] + " · " + historic["Город"] + " · " + historic["Канал"]
        if not historic.empty:
            visual = historic[["Время МСК", "Цена за уп.", "Серия", "Товар", "Площадка", "Город"]].rename(
                columns={"Время МСК": "observed", "Цена за уп.": "amount"}
            )
            window = st.selectbox("Период истории", ["90 дней", "30 дней", "Вся история"])
            if window != "Вся история":
                days = 90 if window == "90 дней" else 30
                cutoff = visual["observed"].max().normalize() - pd.Timedelta(days=days - 1)
                visual = visual[visual["observed"] >= cutoff]
            with alt.data_transformers.enable("default", max_rows=None):
                chart(alt.Chart(visual).mark_line(point=True).encode(
                    x=alt.X("observed:T", title="Дата и время, МСК"),
                    y=alt.Y("amount:Q", title="Цена, ₽", scale=alt.Scale(zero=False)),
                    color=alt.Color("Серия:N", scale=alt.Scale(range=PALETTE), title=None),
                    tooltip=["Товар", "Площадка", "Город", alt.Tooltip("observed:T", title="Время МСК",
                                                                 format="%d.%m.%Y %H:%M"),
                             alt.Tooltip("amount:Q", title="Цена, ₽", format=".2f")],
                ))
            if visual.groupby("Серия").size().max() < 2:
                st.caption("По одному замеру на серию. Линии появятся после следующих проверок.")
    else:
        st.caption("История появляется после первого успешного сбора.")
    with st.expander("Все проверки и ссылки", expanded=True):
        table(current)

with review_tab:
    texts = filters(data("Тексты отзывов"), geo=False)
    st.subheader("Тексты отзывов покупателей")
    if texts.empty:
        st.info("В этом отчёте ещё нет полных текстов отзывов. Сформируйте новый отчёт обновлённым агентом.")
    else:
        source_options = sorted(texts["Аптека"].dropna().unique())
        review_sources = st.multiselect("Аптеки с отзывами", source_options, default=source_options)
        review_search = st.text_input("Поиск по тексту отзыва")
        texts = texts[texts["Аптека"].isin(review_sources)]
        if review_search:
            texts = texts[texts["Текст отзыва"].fillna("").str.contains(review_search, case=False, regex=False)]
        st.caption(f"Отзывов в таблице: {len(texts)}. Одна строка соответствует одному собранному отзыву.")
        table(texts.drop(columns=["SKU", "Бренд"]), row_height=100)
    st.subheader("Анализ тем")
    reviews = filters(data("Отзывы по темам"), geo=False)
    if reviews.empty:
        st.info("В этом отчёте нет раздела отзывов.")
    else:
        counts = reviews.drop_duplicates("SKU")
        left, right = st.columns(2)
        left.metric("Собрано отзывов", int(counts["Всего в базе"].fillna(0).sum()))
        right.metric("Размечено Luna", int(counts["Размечено"].fillna(0).sum()))
        st.caption("Выборка отзывов относится к карточкам товара. Один отзыв может содержать несколько тем.")
        labeled = reviews.dropna(subset=["Доля"])
        if labeled.empty:
            st.info("Темы пока не размечены. Задайте OPENAI_API_KEY и запустите агент без --no-ai.")
        else:
            st.subheader("Доля отзывов с темой")
            chart(grouped_bars(labeled, "Тема", "Доля", "Товар", ".0%", [0, 1]))
        table(reviews)
        with st.expander("Отзывы и цитаты"):
            table(filters(data("Выборка отзывов"), geo=False))

with visibility_tab:
    visibility = filters(data("Видимость"))
    positions = filters(data("Позиции поиска"))
    if visibility.empty or visibility["Проверено"].fillna(0).sum() == 0:
        st.info("Проверенных позиций пока нет. Импортируйте CSV или сохранённый HTML выдачи командой агента.")
    else:
        st.subheader("Доля попаданий в топ‑10")
        observed = visibility.dropna(subset=["Доля топ‑10"])
        if city == "Все города":
            observed = observed.assign(**{"Витрина": observed["Аптека"] + " · " + observed["Город"]})
        if not observed.empty:
            chart(grouped_bars(observed, "Товар", "Доля топ‑10", "Витрина" if city == "Все города" else "Аптека",
                               ".0%", [0, 1]))
    table(visibility)
    with st.expander("Позиции по запросам"):
        table(positions)
    with st.expander("Потери видимости"):
        table(filters(data("Потери видимости")), "В этом срезе нет зафиксированных потерь видимости.")

with audit_tab:
    audit = filters(data("Аудит карточек"), geo=False)
    if not audit.empty:
        priorities = ["Высокий", "Средний", "Низкий"]
        shown = st.multiselect("Приоритет", priorities, default=priorities)
        audit = audit[audit["Приоритет"].isin(shown)]
        if not audit.empty:
            summary = audit.groupby(["Поле", "Приоритет"]).size().reset_index(name="Расхождений")
            st.subheader("Расхождения по полям карточки")
            chart(grouped_bars(summary, "Поле", "Расхождений", "Приоритет", "d"))
    table(audit, "В этой выборке нет записей аудита.")

with promo_tab:
    promos = filters(data("Календарь промо"))
    frequency = filters(data("Частота промо"))
    if not promos.empty:
        st.subheader("Календарь промо")
        visual = promos.copy()
        for name in ["Впервые замечена", "Последний раз"]:
            visual[name] = pd.to_datetime(visual[name], errors="coerce")
        visual = visual.dropna(subset=["Впервые замечена", "Последний раз"])
        visual["Конец интервала"] = visual["Последний раз"] + pd.Timedelta(days=1)
        visual["Предложение"] = visual["Бренд"] + " · " + visual["Аптека"] + " · " + visual["Название"]
        if city == "Все города":
            visual["Предложение"] = visual["Город"] + " · " + visual["Предложение"]
        if not visual.empty:
            span = (visual["Конец интервала"].max() - visual["Впервые замечена"].min()).days
            tick_unit = "day" if span <= 14 else "week" if span <= 100 else "month"
            chart(alt.Chart(visual).mark_bar(cornerRadius=3).encode(
                x=alt.X("Впервые замечена:T", title="Даты наблюдений",
                        axis=alt.Axis(format="%d.%m", tickCount=alt.TimeIntervalStep(interval=tick_unit, step=1))),
                x2="Конец интервала:T",
                y=alt.Y("Предложение:N", title=None, sort="-x", axis=alt.Axis(labelLimit=235)),
                color=alt.Color("Механика:N", scale=alt.Scale(range=PALETTE)),
                tooltip=["Аптека", "Товар", "Город", "Название", "Механика",
                         alt.Tooltip("Впервые замечена:T", format="%d.%m.%Y"),
                         alt.Tooltip("Последний раз:T", format="%d.%m.%Y"), alt.Tooltip("Скидка:Q", format=".1%")],
            ), min(600, max(190, 25 * visual["Предложение"].nunique())))
            st.caption("Полоса соединяет первую и последнюю фиксации. Число подтверждённых дней указано в таблице.")
    table(promos)
    st.subheader("Частота за 30 дней")
    if not frequency.empty:
        observed = frequency[frequency["Проверенных дней"].fillna(0) > 0]
        if city == "Все города":
            observed = observed.assign(**{"Витрина": observed["Аптека"] + " · " + observed["Город"]})
        if not observed.empty:
            chart(grouped_bars(observed, "Товар", "Дней с промо", "Витрина" if city == "Все города" else "Аптека",
                               "d"), height=230)
    table(frequency)

with check_tab:
    log = data("Диагностика исследования")
    if not log.empty and "SKU" in log:
        log = log[log["SKU"].isna() | (log["SKU"] == "") | log["SKU"].isin(selected_skus)]
    table(log, "В этом отчёте нет отдельного листа диагностики исследований.")


with internet_tab:
    render_internet(DATA_DIR, selected_brands, api_key, table, chart)
