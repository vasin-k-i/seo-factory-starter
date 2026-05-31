"""
PageSpeed Insights client.

Использование:
    from modules.pagespeed import psi_run, psi_summary

    data = psi_run("https://example.com/", strategy="mobile")
    summary = psi_summary(data)
    print(summary)

ENV:
    PSI_API_KEY — Google Cloud API key с разрешённым PageSpeed Insights API.

Лимиты: 25 000 запросов/день (бесплатно).
"""

from __future__ import annotations

import os
import logging
import requests
from typing import Literal

log = logging.getLogger(__name__)

PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

Strategy = Literal["mobile", "desktop"]
Category = Literal["performance", "accessibility", "best-practices", "seo", "pwa"]


def _api_key() -> str:
    k = os.environ.get("PSI_API_KEY", "").strip()
    if not k:
        raise RuntimeError(
            "PSI_API_KEY не задан. См. docs/seo/agent-system/access-checklist.md, пункт 4."
        )
    return k


def psi_run(
    url: str,
    strategy: Strategy = "mobile",
    categories: list[Category] | None = None,
    locale: str = "ru",
) -> dict:
    """Запросить отчёт PSI для URL. Возвращает сырой ответ."""
    params = [
        ("url", url),
        ("key", _api_key()),
        ("strategy", strategy),
        ("locale", locale),
    ]
    for cat in categories or ["performance", "seo", "accessibility", "best-practices"]:
        params.append(("category", cat))

    response = requests.get(PSI_URL, params=params, timeout=60)
    response.raise_for_status()
    return response.json()


def psi_summary(data: dict) -> dict:
    """Извлечь ключевые метрики из ответа PSI: Core Web Vitals + категориальные scores."""
    lh = data.get("lighthouseResult", {})
    audits = lh.get("audits", {})
    categories = lh.get("categories", {})

    def metric(audit_id: str, field: str = "numericValue") -> float | None:
        v = audits.get(audit_id, {}).get(field)
        return v

    def category_score(cat_id: str) -> int | None:
        s = categories.get(cat_id, {}).get("score")
        return round(s * 100) if isinstance(s, (int, float)) else None

    return {
        "final_url": lh.get("finalUrl"),
        "fetched_at": lh.get("fetchTime"),
        "strategy": lh.get("configSettings", {}).get("formFactor"),
        # Core Web Vitals (в мс, кроме CLS)
        "lcp_ms": metric("largest-contentful-paint"),
        "inp_ms": metric("interaction-to-next-paint"),
        "cls": metric("cumulative-layout-shift"),
        "fcp_ms": metric("first-contentful-paint"),
        "ttfb_ms": metric("server-response-time"),
        "tbt_ms": metric("total-blocking-time"),
        "speed_index_ms": metric("speed-index"),
        # Категориальные scores (0–100)
        "performance": category_score("performance"),
        "accessibility": category_score("accessibility"),
        "best_practices": category_score("best-practices"),
        "seo": category_score("seo"),
    }


def _fmt_ms(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v / 1000:.2f}s" if v > 1000 else f"{v:.0f}ms"


def _fmt_score(v: int | None) -> str:
    if v is None:
        return "—"
    icon = "🟢" if v >= 90 else "🟡" if v >= 50 else "🔴"
    return f"{icon} {v}"


def print_summary(s: dict) -> None:
    print(f"  URL:       {s['final_url']}")
    print(f"  Strategy:  {s['strategy']}")
    print(f"  Time:      {s['fetched_at']}")
    print(f"  --- Core Web Vitals ---")
    print(f"  LCP:       {_fmt_ms(s['lcp_ms'])}    (порог <2.5s)")
    print(f"  INP:       {_fmt_ms(s['inp_ms'])}    (порог <200ms)")
    print(f"  CLS:       {s['cls']:.3f}            (порог <0.1)")
    print(f"  FCP:       {_fmt_ms(s['fcp_ms'])}")
    print(f"  TTFB:      {_fmt_ms(s['ttfb_ms'])}")
    print(f"  TBT:       {_fmt_ms(s['tbt_ms'])}")
    print(f"  --- Lighthouse Categories (0–100) ---")
    print(f"  Performance:    {_fmt_score(s['performance'])}")
    print(f"  Accessibility:  {_fmt_score(s['accessibility'])}")
    print(f"  Best Practices: {_fmt_score(s['best_practices'])}")
    print(f"  SEO:            {_fmt_score(s['seo'])}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    URLS = [
        "https://example.com/",
        "https://example.com/doshkolnoe",
        "https://example.com/blog/zarplata-vospitatelya-2026",
    ]

    for url in URLS:
        for strategy in ("mobile", "desktop"):
            print(f"\n{'=' * 60}")
            print(f"PSI · {strategy.upper()} · {url}")
            print("=" * 60)
            try:
                data = psi_run(url, strategy=strategy)
                summary = psi_summary(data)
                print_summary(summary)
            except Exception as e:
                print(f"⚠ Ошибка: {e}")
