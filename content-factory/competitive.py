"""L2 «редакторский обгон»: SERP top-N + фетч текста конкурентов + дедуп.

Изолированный модуль — импортируется ЛЕНИВО из orchestrator только при
L2_COMPETITIVE=1. Любая ошибка → пустой результат, фабрика тихо откатывается к L1.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional
from urllib.parse import urlparse

import requests

log = logging.getLogger("competitive")

UA = os.environ.get("COMPETITIVE_UA", "Mozilla/5.0 (compatible; seo-factory-starter/1.0)")
# Свой(и) домен(ы) — не разбираем сами себя как конкурента. Задать через env
# SITE_DOMAIN (можно несколько через запятую), напр. SITE_DOMAIN=mysite.ru,www.mysite.ru
OUR_DOMAINS = tuple(
    d.strip().lower()
    for d in os.environ.get("SITE_DOMAIN", "example.com").split(",")
    if d.strip()
)


def _load_yandex_creds() -> tuple[str, str]:
    """API-ключ + folderId Яндекс.Облака из env (веб-поиск = тот же сервис-аккаунтный
    ключ, что и Wordstat; у ключа должен быть scope веб-поиска)."""
    return (
        os.environ.get("YANDEX_CLOUD_API_KEY", ""),
        os.environ.get("YC_FOLDER_ID", ""),
    )


def _serp_yandex_cloud(query: str, n: int = 3, *, timeout: int = 25) -> list[str]:
    """top-N органических URL из Яндекса через Yandex Cloud Search API (async).

    Тот же ключ, что и Wordstat (scope веб-поиска включён) — без доп. подписки.
    Async: POST searchAsync → операция → poll → base64-XML → <url>. Гео РФ задаётся
    параметром (SEARCH_TYPE_RU), не IP раннера. [] при любой ошибке → L2 → L1.
    """
    key, folder = _load_yandex_creds()
    if not key or not folder:
        log.warning("Yandex Cloud ключ/folder не заданы — SERP пропущен")
        return []
    try:
        import base64
        import time as _t
        r = requests.post(
            "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync",
            headers={"Authorization": f"Api-Key {key}", "Content-Type": "application/json"},
            json={"query": {"searchType": "SEARCH_TYPE_RU", "queryText": query},
                  "folderId": folder, "responseFormat": "FORMAT_XML"},
            timeout=timeout,
        )
        r.raise_for_status()
        op_id = r.json().get("id")
        if not op_id:
            return []
        deadline = _t.monotonic() + timeout
        raw = None
        while _t.monotonic() < deadline:
            _t.sleep(2)
            op = requests.get(
                f"https://operation.api.cloud.yandex.net/operations/{op_id}",
                headers={"Authorization": f"Api-Key {key}"}, timeout=timeout,
            ).json()
            if op.get("error"):
                log.warning("Yandex SERP error: %s", op["error"])
                return []
            if op.get("done"):
                raw = (op.get("response") or {}).get("rawData")
                break
        if not raw:
            return []
        xml = base64.b64decode(raw).decode("utf-8", "replace")
        return re.findall(r"<url>(.*?)</url>", xml)[:n]
    except Exception as e:  # noqa: BLE001
        log.warning("_serp_yandex_cloud(%r) failed: %s", query, e)
        return []


def _serp_serpapi(query: str, n: int = 3, *, timeout: int = 20) -> list[str]:
    """top-N органических URL Яндекса через SerpApi (если задан SERPAPI_KEY)."""
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        return []
    try:
        resp = requests.get(
            "https://serpapi.com/search",
            params={"engine": "yandex", "text": query, "yandex_domain": "yandex.ru",
                    "lr": 225, "api_key": api_key},
            timeout=timeout,
        )
        resp.raise_for_status()
        organic = resp.json().get("organic_results", []) or []
        organic.sort(key=lambda r: r.get("position", 1_000_000))
        return [r["link"] for r in organic if r.get("link")][:n]
    except Exception as e:  # noqa: BLE001
        log.warning("_serp_serpapi(%r) failed: %s", query, e)
        return []


def serp_top(query: str, n: int = 3, *, timeout: int = 20) -> list[str]:
    """top-N органических URL из Яндекс-выдачи (РФ).

    Провайдеры по приоритету: SerpApi (если задан SERPAPI_KEY) → Yandex Cloud
    Search API (ключ Wordstat, без доп. подписки). [] при любой ошибке.
    """
    if os.environ.get("SERPAPI_KEY"):
        urls = _serp_serpapi(query, n, timeout=timeout)
        if urls:
            return urls
    return _serp_yandex_cloud(query, n, timeout=max(timeout, 25))


def dedup_our_domains(urls: list[str], *, blocklist: tuple[str, ...] = OUR_DOMAINS) -> list[str]:
    """Выкинуть наши/аффилированные домены ДО фетча (по подстроке в netloc)."""
    out: list[str] = []
    for u in urls:
        host = (urlparse(u).netloc or "").lower()
        if any(b in host for b in blocklist):
            continue
        out.append(u)
    return out


def fetch_competitor_text(url: str, *, max_chars: int = 8000, timeout: int = 20) -> Optional[dict]:
    """Скачать статью конкурента, вытащить основной текст + H2.

    requests + BeautifulSoup (JS не исполняется). None при HTTP>=400/пусто/ошибке —
    тогда источник просто не учитывается.
    """
    try:
        from bs4 import BeautifulSoup  # ленивый импорт (bs4 ставится только под L2)
    except Exception as e:  # noqa: BLE001
        log.warning("bs4 недоступен: %s", e)
        return None
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code >= 400 or not r.text:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
            tag.decompose()
        title = (soup.title.text.strip() if soup.title else "")[:200]
        h2s = [h.get_text(" ", strip=True)[:120] for h in soup.find_all("h2")][:12]
        container = soup.find("article") or soup.find("main") or soup.body
        imgs = container.find_all("img") if container else []
        tables = container.find_all("table") if container else []
        svgs = container.find_all("svg") if container else []
        text = container.get_text(" ", strip=True) if container else ""
        text = " ".join(text.split())
        if len(text) < 200:
            return None
        has_infographic = bool(svgs) or any(
            "infograph" in ((img.get("src") or "") + " " + (img.get("alt") or "")).lower()
            for img in imgs
        )
        return {
            "url": url,
            "domain": (urlparse(url).netloc or "").lower(),
            "title": title,
            "h2": h2s,
            "length_chars": len(text),           # аудит C4: длина статьи в символах
            "images_count": len(imgs),           # аудит C4: число изображений
            "has_tables": bool(tables),          # аудит C4: сводные таблицы
            "has_infographic": has_infographic,  # аудит C4: инфографика
            "text": text[:max_chars],
        }
    except Exception as e:  # noqa: BLE001
        log.warning("fetch_competitor_text(%s) failed: %s", url, e)
        return None


def collect_competitive(topic: Any, *, n: int | None = None) -> list[dict]:
    """SERP → dedup → fetch для одной темы. Целиком в try/except → [] при сбое.

    Реверс-инжиниринг всей первой страницы: тянем топ-10 URL (SERP_TOP_N), выкидываем
    свои домены/мёртвые, глубоко разбираем до COMPETITIVE_N конкурентов (по умолч. 3 —
    объём контекста/цена). Больше глубина → точнее «консенсус выдачи», дороже.
    """
    if n is None:
        n = int(os.environ.get("COMPETITIVE_N", "3") or "3")
    try:
        serp_n = max(n + 2, int(os.environ.get("SERP_TOP_N", "10") or "10"))
        urls = serp_top(topic.primary_keyword, n=serp_n)  # топ выдачи, запас на дедуп/мёртвые
        urls = dedup_our_domains(urls)
        sources: list[dict] = []
        for u in urls:
            if len(sources) >= n:
                break
            src = fetch_competitor_text(u)
            if src:
                sources.append(src)
        log.info("competitive[%s]: %d разобрано из %d URL выдачи",
                 topic.primary_keyword, len(sources), len(urls))
        return sources
    except Exception as e:  # noqa: BLE001
        log.warning("collect_competitive(%r) failed: %s", getattr(topic, "primary_keyword", "?"), e)
        return []


_RU_STOP = frozenset(
    "и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только "
    "ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни "
    "быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они тут где "
    "есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет "
    "тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы "
    "нее сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над "
    "больше тот через эти нас про всего них какая много разве сколько всю между это чем этом "
    "которые которых также этих свои этом более очень весь всё этот такие этими нужно можно".split()
)


def extract_lsi(sources: list[dict], *, primary: str = "", top: int = 25) -> list[str]:
    """Топ-N LSI-слов из текстов конкурентов (аудит C2): частые содержательные слова
    минус стоп-слова и слова основного ключа. Без Wordstat — из уже скачанных текстов."""
    import re as _re
    from collections import Counter
    stop = set(_RU_STOP) | {w.lower() for w in _re.findall(r"\w+", primary or "")}
    cnt: Counter = Counter()
    for s in sources:
        for w in _re.findall(r"[а-яёa-z]{4,}", (s.get("text") or "").lower()):
            if w not in stop:
                cnt[w] += 1
    return [w for w, _ in cnt.most_common(top)]


def competitive_user(topic: Any, sources: list[dict], *, max_total_chars: int = 18000) -> str:
    """user-текст для стадии 00_competitive: тема + усечённые источники.

    Общий бюджет символов (защита контекста/цены) — режем суммарно, не по одному.
    """
    # Аудит C4: агрегаты по конкурентам для расчёта объёма/медиа статьи.
    lengths = [s.get("length_chars", 0) for s in sources]
    imgs = [s.get("images_count", 0) for s in sources]
    avg_len = int(sum(lengths) / len(lengths)) if lengths else 0
    target_images = int(sum(imgs) / len(imgs) * 1.15 + 0.999) if imgs else 0  # среднее +15%, ceil
    lsi = extract_lsi(sources, primary=getattr(topic, "primary_keyword", ""))  # аудит C2
    parts = [
        f"TOPIC: {topic.topic}",
        f"PRIMARY_KEYWORD: {topic.primary_keyword}",
        f"SECONDARY_KEYWORDS: {getattr(topic, 'secondary_keywords', '')}",
        "",
        "МЕТРИКИ КОНКУРЕНТОВ (аудит C4 — заложи в бриф): "
        f"target_length_chars≈{avg_len}, target_images≈{target_images} (среднее +15%), "
        f"needs_infographic={any(s.get('has_infographic') for s in sources)}, "
        f"needs_tables={any(s.get('has_tables') for s in sources)}",
        "",
        ("LSI-СЛОВА (аудит C2 — естественно вплести в текст): " + ", ".join(lsi)) if lsi else "",
        f"КОНКУРЕНТЫ ИЗ ВЫДАЧИ (top-{len(sources)}):",
    ]
    budget = max_total_chars
    for i, s in enumerate(sources, 1):
        chunk = s["text"][: max(0, budget)]
        budget -= len(chunk)
        parts.append(
            f"\n--- Источник {i}: {s['domain']} ---\n"
            f"TITLE: {s['title']}\n"
            f"H2: {' | '.join(s['h2'])}\n"
            f"МЕТРИКИ: длина {s.get('length_chars', 0)} симв, картинок {s.get('images_count', 0)}, "
            f"таблицы {s.get('has_tables', False)}, инфографика {s.get('has_infographic', False)}\n"
            f"TEXT: {chunk}\n"
        )
        if budget <= 0:
            break
    return "\n".join(parts)


def parse_competitive(raw: str) -> dict:
    """Распарсить JSON-ответ стадии 00 (снять возможную ```json-обёртку)."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    return json.loads(raw)
