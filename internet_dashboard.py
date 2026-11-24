import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from internet_monitor import TABLES, collect, load_config, write_snapshot
from internet_sources import canonical
from internet_channels import source_category


def load_report(data_dir):
    current = data_dir / "internet_snapshot.json"
    example = Path(__file__).resolve().parent / "example" / "internet_snapshot.json"
    path = current if current.exists() else example
    if not path.exists():
        return None, False
    return json.loads(path.read_text(encoding="utf-8")), path == example


def selected_rows(report, name, brands):
    frame = pd.DataFrame(report["tables"].get(name, []), columns=TABLES[name])
    if "Бренд" in frame:
        frame = frame[frame["Бренд"].isin(brands)]
    return frame


def render(data_dir, selected_brands, api_key, table, chart):
    settings_path = data_dir / "internet_settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        config = load_config(comparison=settings.get("comparison_brands"))
        for url in settings.get("urls", []):
            config["sources"].append({"url": canonical(url), "brands": [b["name"] for b in config["brands"]], "kind": "index"})
    except (OSError, ValueError, TypeError, KeyError):
        st.error("Не удалось прочитать настройки интернет-мониторинга.")
        return
    st.subheader("Материалы о продуктах в интернете")
    st.caption("Подтверждённые страницы, вопросы, промопредложения и новые наблюдения ассортимента со ссылками.")
    with st.expander("Источники и бренды для сравнения"):
        st.dataframe(pd.DataFrame(config["sources"])[["url", "kind"]].rename(
            columns={"url": "Источник", "kind": "Тип"}), hide_index=True, width="stretch")
        if config.get("channels"):
            st.dataframe(pd.DataFrame([
                {"Канал": c["name"], "Категория": c["category"], "Домены": ", ".join(c["domains"])}
                for c in config["channels"]
            ]), hide_index=True, width="stretch")
        st.caption("Threads: индексируемые ссылки ищутся через поисковые API. Прямой поиск постов требует "
                   "THREADS_ACCESS_TOKEN и одобренного разрешения threads_keyword_search.")
        with st.form("internet_settings"):
            comparison = st.text_area("Бренды для сравнения", value="\n".join(
                brand["name"] for brand in config.get("comparison_brands", [])
            ), help="По одному названию в строке. Для новых внешних брендов включите поиск источников.")
            urls = st.text_area("Дополнительные публичные страницы", value="\n".join(settings.get("urls", [])),
                               help="По одной HTTP(S)-ссылке в строке.")
            saved = st.form_submit_button("Сохранить источники")
        if saved:
            try:
                comparisons = list(dict.fromkeys(name.strip() for name in comparison.splitlines() if name.strip()))
                if len(comparisons) > 20 or any(not 3 <= len(name) <= 80 for name in comparisons):
                    raise ValueError("Допускается до 20 названий длиной от 3 до 80 символов.")
                extra = list(dict.fromkeys(canonical(url) for url in urls.splitlines() if url.strip()))
                if len(extra) > 30:
                    raise ValueError("Допускается до 30 дополнительных источников.")
                primary = {b["name"] for b in config["brands"]}
                others = [{"name": name, "aliases": [name], "role": "Для сравнения", "domains": []}
                          for name in comparisons if name not in primary]
                load_config(comparison=others)
                data_dir.mkdir(parents=True, exist_ok=True)
                settings_path.write_text(json.dumps({"comparison_brands": others, "urls": extra},
                                                    ensure_ascii=False, indent=2), encoding="utf-8")
                st.rerun()
            except (ValueError, OSError) as error:
                st.error(str(error))

    providers = st.multiselect("Поисковые API", ["auto", "google", "yandex"],
                               default=config.get("search_providers", ["auto"]),
                               format_func=lambda value: {"auto": "OpenAI / Brave", "google": "Google / SerpApi",
                                                          "yandex": "Яндекс / SerpApi"}[value])
    config["search_providers"] = providers
    if set(providers) & {"google", "yandex"} and not os.getenv("SERPAPI_API_KEY"):
        st.caption("Google и Яндекс: задайте SERPAPI_API_KEY в Secrets или окружении.")
    token_present = bool(os.getenv("THREADS_ACCESS_TOKEN"))
    config["threads_enabled"] = st.checkbox("Получать публичные посты через Threads API", value=token_present and config.get("threads_enabled", True),
                                            disabled=not token_present)
    available_search = bool(api_key or os.getenv("BRAVE_SEARCH_API_KEY") or os.getenv("SERPAPI_API_KEY")
                            or (token_present and config["threads_enabled"]))
    search = st.checkbox("Искать новые страницы через API", value=available_search,
                         disabled=not available_search,
                         help="OpenAI, Brave и SerpApi используют свои ключи и тарифы. Threads API подключается отдельным токеном.")
    st.caption(f"За запуск — до {config['max_pages']} страниц, {config['max_discovery_queries']} веб-поисковых запросов "
               f"суммарно по провайдерам и {config.get('max_threads_queries', 2)} запросов Threads при включённом API. "
               "Группы источников и бренды проверяются по очереди между запусками.")
    if st.button("Обновить интернет-мониторинг"):
        try:
            with st.spinner("Проверяю публичные источники…"):
                report = collect(config, data_dir / "internet.sqlite3", api_key=api_key,
                                 use_search=search, no_ai=not bool(api_key))
                write_snapshot(data_dir / "internet_snapshot.json", report)
            st.success("Интернет-материалы обновлены. Результат проверки каждого источника доступен ниже.")
        except (OSError, ValueError, KeyError, sqlite3.Error) as error:
            st.error(str(error))

    try:
        report, example = load_report(data_dir)
    except (OSError, ValueError):
        st.error("Не удалось прочитать сохранённые интернет-материалы.")
        return
    if report is None:
        st.info("Нажмите «Обновить интернет-мониторинг», чтобы выполнить первый сбор.")
        return
    if example:
        st.info("Пример сбора из архива. Для свежих данных обновите интернет-мониторинг.")
    st.caption("Срез сформирован " + report["generated_at"])
    all_names = [brand["name"] for brand in config["all_brands"]]
    defaults = list(dict.fromkeys([*selected_brands, *(b["name"] for b in config.get("comparison_brands", []))]))
    chosen = st.multiselect("Бренды интернет-мониторинга", all_names, default=[b for b in defaults if b in all_names])
    mentions = selected_rows(report, "Упоминания интернета", chosen)
    if not mentions.empty:
        mentions["Дата обнаружения"] = pd.to_datetime(mentions["Обнаружено"], utc=True, errors="coerce").dt.tz_convert("Europe/Moscow")
    cols = st.columns(3)
    cols[0].metric("Материалов без перепечаток", mentions["Группа"].nunique() if len(mentions) else 0)
    cols[1].metric("Ссылок", len(mentions))
    cols[2].metric("Площадок", mentions["Домен"].nunique() if len(mentions) else 0)
    tabs = st.tabs(["Упоминания", "Вопросы", "Материалы", "Реклама", "Поиск", "Ассортимент", "Сравнение брендов"])
    with tabs[0]:
        if not mentions.empty:
            channels = config.get("channels", [])
            labels = [source_category(url, channels) for url in mentions["Источник"]]
            mentions["Категория источника"] = [label[0] for label in labels]
            mentions["Канал"] = [label[1] for label in labels]
            categories = st.multiselect("Категории в таблице упоминаний",
                                        sorted(mentions["Категория источника"].unique()),
                                        default=sorted(mentions["Категория источника"].unique()))
            mentions = mentions[mentions["Категория источника"].isin(categories)]
        if not mentions.empty:
            timeline = mentions.drop_duplicates("Группа").copy()
            timeline["День"] = timeline["Дата обнаружения"].dt.strftime("%Y-%m-%d")
            trend = timeline.groupby("День").size().reset_index(name="Найдено материалов")
            chart(alt.Chart(trend).mark_bar(color="#125965").encode(
                x=alt.X("День:O", title="День обнаружения", axis=alt.Axis(labelAngle=-25)),
                y=alt.Y("Найдено материалов:Q", title="Уникальных материалов"), tooltip=["День", "Найдено материалов"]
            ), 230)
        table(mentions.drop(columns=["Группа", "Дата обнаружения"], errors="ignore"))
        with st.expander("Методика и покрытие"):
            for value in report.get("methodology", []):
                st.write(value)
    with tabs[1]:
        questions = selected_rows(report, "Вопросы покупателей", chosen)
        text = st.text_input("Найти вопрос")
        if text and not questions.empty:
            questions = questions[questions["Вопрос"].str.contains(text, case=False, regex=False)]
        table(questions)
        st.caption("Включены опубликованные вопросы и FAQ. Одинаковые вопросы внутри бренда объединяются; частота спроса не измеряется.")
    with tabs[2]:
        materials = selected_rows(report, "Материалы о продуктах", chosen)
        if not materials.empty:
            kinds = st.multiselect("Тип материала", sorted(materials["Тип"].dropna().unique()),
                                   default=sorted(materials["Тип"].dropna().unique()))
            materials = materials[materials["Тип"].isin(kinds)]
        table(materials)
    with tabs[3]:
        ads = selected_rows(report, "Реклама в интернете", chosen)
        if not ads.empty:
            summary = ads.groupby(["Бренд", "Механика"]).size().reset_index(name="Предложений")
            chart(alt.Chart(summary).mark_bar().encode(
                x=alt.X("Бренд:N", title=None, axis=alt.Axis(labelAngle=-15)),
                y=alt.Y("Предложений:Q"), color="Механика:N", tooltip=["Бренд", "Механика", "Предложений"]
            ), 230)
        table(ads)
        st.caption("Сохранены опубликованные формулировки предложений. Видимость баннеров и рекламные расходы не измеряются.")
    with tabs[4]:
        search_rows = selected_rows(report, "Поиск в интернете", chosen)
        table(search_rows, "Нет поисковых срезов. Включите API-поиск либо импортируйте выдачу через CLI.")
        st.caption("Числовые позиции есть у Brave Search, Google / Яндекс через SerpApi и импортированной выдачи. "
                   "Для источников OpenAI позиция остаётся пустой.")
        if not search_rows.empty:
            ranked = search_rows[(search_rows["Провайдер"] != "OpenAI web search")
                                 & search_rows["Позиция"].notna()]
            official = ranked[ranked["Официальный домен"] == "Да"]
            table(official, "В загруженных результатах пока нет подтверждённых позиций официальных доменов.")
    with tabs[5]:
        table(selected_rows(report, "Ассортимент интернета", chosen))
        st.caption("Формы и упаковки извлечены из опубликованных названий. Новые строки требуют проверки по карточке.")
    with tabs[6]:
        compared = selected_rows(report, "Интернет конкурентов", chosen)
        if not compared.empty:
            chart(alt.Chart(compared).mark_bar(color="#125965").encode(
                x=alt.X("Бренд:N", axis=alt.Axis(labelAngle=-15), title=None),
                y=alt.Y("Уникальных материалов:Q"),
                tooltip=["Бренд", "Роль", "Уникальных материалов", "Площадок"]
            ), 230)
        table(compared)
        st.caption("Сравнение охватывает только проверенные источники. Дополнительные бренды задаются в настройках.")
    with st.expander("Проверки интернет-источников"):
        table(pd.DataFrame(report["tables"]["Источники интернета"], columns=TABLES["Источники интернета"]))
    st.download_button("Скачать интернет-отчёт JSON", json.dumps(report, ensure_ascii=False, indent=2).encode(),
                       file_name="internet_report.json", mime="application/json")
