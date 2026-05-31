"""
M6 — Competitor watcher.

Что делает:
  1. Краулит главные страницы и /blog/ конкурентов из docs/seo/agent-system/competitors.
  2. Извлекает структуру: title, h1, h2, объём, количество ссылок, наличие FAQ/JSON-LD.
  3. Сравнивает с нашим сайтом (M2-данные).
  4. Если у конкурента появилась новая статья в /blog/ или /news/ за последнюю неделю —
     алертит. Это сигнал, что конкурент ловит горячую тему.
  5. Опционально (если ANTHROPIC_API_KEY и есть баланс): через Claude извлекает
     ключевые ideas из структуры — что нового они показывают.

Запуск:
    python3 -m modules.m6_competitors             # production
    python3 -m modules.m6_competitors --dry-run   # без Telegram

Это не SERP-парсер (нет Я.XML / Topvisor) — это **структурный анализ** сайтов конкурентов.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(THIS_DIR.parent))

from modules.crawler import crawl, fetch_sitemap_urls  # noqa: E402

log = logging.getLogger(__name__)

REPO_ROOT = THIS_DIR.parent.parent
DATA_DIR = THIS_DIR.parent / "data" / "competitors"

# Из docs/seo/keywords.md + SEO-исследования (synergy, distant-college, niidpo).
COMPETITORS = [
    {
        "name": "Synergy",
        "homepage": "https://synergy.ru/about/education_articles/kolledzh/",
        "blog_url": "https://synergy.ru/journal",
        "sitemap": "https://synergy.ru/sitemap.xml",
    },
    {
        "name": "Distant-college (НСПК)",
        "homepage": "https://distant-college.ru/",
        "blog_url": "https://distant-college.ru/blog",
        "sitemap": "https://distant-college.ru/sitemap.xml",
    },
    {
        "name": "NIIDPO",
        "homepage": "https://niidpo.ru/",
        "blog_url": "https://niidpo.ru/blog/",
        "sitemap": "https://niidpo.ru/sitemap.xml",
    },
    {
        "name": "VuzoPedia SPO",
        "homepage": "https://vuzopedia.ru/spo/journal",
        "blog_url": "https://vuzopedia.ru/spo/journal",
        "sitemap": "https://vuzopedia.ru/sitemap.xml",
    },
]


def analyse_page(url: str) -> dict:
    """Структурный анализ одной страницы."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {"error": "bs4 not installed"}

    result = crawl(url)
    if result.error or result.status >= 400 or not result.html:
        return {"error": result.error or f"HTTP {result.status}"}

    soup = BeautifulSoup(result.html, "html.parser")

    title_tag = soup.find("title")
    h1_tag = soup.find("h1")
    h2_tags = soup.find_all("h2")
    img_tags = soup.find_all("img")
    a_tags = soup.find_all("a", href=True)

    json_ld_scripts = soup.find_all("script", type="application/ld+json")
    json_ld_types: list[str] = []
    for s in json_ld_scripts:
        try:
            data = json.loads((s.string or s.text or "").strip())
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and "@type" in item:
                    t = item["@type"]
                    if isinstance(t, list):
                        json_ld_types.extend(t)
                    else:
                        json_ld_types.append(t)
        except Exception:
            pass

    body_text = soup.body.get_text(" ", strip=True) if soup.body else ""
    word_count = len(re.findall(r"\b[\w-]+\b", body_text))

    return {
        "url": url,
        "status": result.status,
        "title": (title_tag.text.strip() if title_tag else "").strip()[:200],
        "h1": (h1_tag.text.strip() if h1_tag else "").strip()[:200],
        "h2_count": len(h2_tags),
        "h2_examples": [h.text.strip()[:80] for h in h2_tags[:5]],
        "img_count": len(img_tags),
        "links_count": len(a_tags),
        "json_ld_types": sorted(set(json_ld_types)),
        "word_count": word_count,
    }


def find_recent_blog_urls(sitemap_url: str, days: int = 30, limit: int = 100) -> list[str]:
    """Найти URLs из sitemap, по которым lastmod моложе N дней (не все sitemap это даёт)."""
    try:
        from bs4 import BeautifulSoup
        import requests
        r = requests.get(sitemap_url, timeout=20,
                         headers={"User-Agent": "seo-agent/1.0"})
        if r.status_code >= 400:
            return []
        soup = BeautifulSoup(r.content, "xml")
        cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        out: list[str] = []
        for url in soup.find_all("url"):
            loc = url.find("loc")
            if not loc:
                continue
            url_text = loc.text.strip()
            lastmod = url.find("lastmod")
            mod_date = lastmod.text.strip()[:10] if lastmod else ""
            # Берём только URL, похожие на блог/статью
            path = urlparse(url_text).path.lower()
            if any(seg in path for seg in ("/blog/", "/news/", "/journal/", "/articles/")):
                if not mod_date or mod_date >= cutoff:
                    out.append(url_text)
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        log.warning("Sitemap %s: %s", sitemap_url, e)
        return []


def run_competitors(dry_run: bool = False) -> Path:
    today = dt.date.today().isoformat()
    log.info("=== M6 competitor watcher · %s ===", today)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    report: dict = {"date": today, "competitors": []}
    for comp in COMPETITORS:
        log.info("Анализирую %s — %s", comp["name"], comp["homepage"])
        homepage_data = analyse_page(comp["homepage"])
        recent = find_recent_blog_urls(comp["sitemap"], days=14)
        log.info("  Свежих URL за 14 дней (sitemap): %d", len(recent))
        report["competitors"].append({
            "name": comp["name"],
            "homepage_analysis": homepage_data,
            "recent_urls": recent[:30],
            "recent_count": len(recent),
        })

    path = DATA_DIR / f"{today}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Снимок: %s", path)

    # Telegram сводка
    if not dry_run and os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        from notifiers.telegram import send_telegram
        lines = [f"👁 Что у конкурентов · {today}"]
        lines.append("\nСмотрим, как часто конкуренты выпускают статьи и насколько "
                     "наполнены их главные страницы — чтобы держать темп.")
        for c in report["competitors"]:
            recent = c.get("recent_count", 0)
            home = c.get("homepage_analysis", {})
            error = home.get("error")
            if error:
                lines.append(f"\n{c['name']}: не удалось проверить ({error})")
                continue
            lines.append(f"\n{c['name']}")
            lines.append(f"  Главная страница: {home.get('word_count', 0)} слов, "
                         f"{home.get('h2_count', 0)} подзаголовков, {home.get('img_count', 0)} картинок")
            lines.append(f"  Новых статей за 2 недели: {recent}")
        send_telegram("\n".join(lines))

    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="M6 competitor watcher")
    parser.add_argument("--dry-run", action="store_true", help="Не слать в Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_competitors(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
