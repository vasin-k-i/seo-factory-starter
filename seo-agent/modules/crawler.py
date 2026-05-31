"""
Краулер по sitemap.xml. Грузит HTML каждой страницы, отдаёт CrawlResult.

Использование:
    from modules.crawler import fetch_sitemap_urls, crawl_all

    urls = fetch_sitemap_urls("https://example.com/sitemap.xml")
    results = crawl_all(urls)
    for r in results:
        if r.status >= 400:
            print(r.url, r.status)

Зачем отдельный модуль: M2 (tech audit) + M6 (competitors) будут реюзить логику.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; seo-agent/1.0; +https://example.com)"
DEFAULT_TIMEOUT = 30
DEFAULT_DELAY = 0.3  # пауза между запросами, чтобы не DDoS-ить свой же сайт


@dataclass
class CrawlResult:
    url: str
    status: int
    final_url: str
    redirects: list[str] = field(default_factory=list)
    html: Optional[str] = None
    content_type: str = ""
    elapsed_ms: int = 0
    error: Optional[str] = None


def fetch_sitemap_urls(sitemap_url: str, timeout: int = DEFAULT_TIMEOUT) -> list[str]:
    """Скачать sitemap.xml и вернуть список URL из <loc>.

    Поддерживает sitemap-index (вложенные sitemap'ы) — рекурсивно разворачивает.
    """
    response = requests.get(
        sitemap_url, timeout=timeout, headers={"User-Agent": DEFAULT_USER_AGENT}
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "xml")

    # sitemap-index: <sitemapindex><sitemap><loc>...
    if soup.find("sitemapindex"):
        urls: list[str] = []
        for sm_loc in soup.find_all("loc"):
            urls.extend(fetch_sitemap_urls(sm_loc.text.strip(), timeout))
        return urls

    return [loc.text.strip() for loc in soup.find_all("loc")]


def crawl(url: str, timeout: int = DEFAULT_TIMEOUT) -> CrawlResult:
    """Загрузить страницу. Не падаем на ошибках сети — пишем error в результат."""
    start = time.perf_counter()
    try:
        response = requests.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept-Language": "ru,en;q=0.5"},
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        content_type = response.headers.get("content-type", "")
        is_html = content_type.startswith("text/html")
        return CrawlResult(
            url=url,
            status=response.status_code,
            final_url=response.url,
            redirects=[h.url for h in response.history],
            html=response.text if is_html else None,
            content_type=content_type,
            elapsed_ms=elapsed_ms,
        )
    except requests.RequestException as e:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return CrawlResult(
            url=url,
            status=0,
            final_url=url,
            elapsed_ms=elapsed_ms,
            error=str(e),
        )


def crawl_all(
    urls: list[str],
    delay: float = DEFAULT_DELAY,
    timeout: int = DEFAULT_TIMEOUT,
    progress_every: int = 10,
) -> list[CrawlResult]:
    results: list[CrawlResult] = []
    total = len(urls)
    for i, url in enumerate(urls, 1):
        if i == 1 or i % progress_every == 0 or i == total:
            log.info("crawl [%d/%d] %s", i, total, url)
        results.append(crawl(url, timeout=timeout))
        if i < total:
            time.sleep(delay)
    return results


if __name__ == "__main__":
    # Smoke-test: краулим sitemap example, выводим сводку.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    SITEMAP = "https://example.com/sitemap.xml"
    print(f"→ Качаю sitemap: {SITEMAP}")
    urls = fetch_sitemap_urls(SITEMAP)
    print(f"  Найдено URL: {len(urls)}")

    # Возьмём первые 5 для быстрого smoke-теста
    sample = urls[:5]
    print(f"\n→ Краулим первые {len(sample)} URL:")
    results = crawl_all(sample, delay=0.2)

    print("\n=== Результат ===")
    for r in results:
        marker = "✓" if 200 <= r.status < 300 else ("⤳" if 300 <= r.status < 400 else "✗")
        redir = f" (через {len(r.redirects)} редирект)" if r.redirects else ""
        print(f"  {marker} {r.status:>3} · {r.elapsed_ms:>4}ms · {r.url}{redir}")
        if r.error:
            print(f"    error: {r.error}")
