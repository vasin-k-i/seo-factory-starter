"""
M2 — еженедельный технический SEO-аудит.

Что делает:
  1. Краулит sitemap.xml.
  2. Запускает все HTML-проверки (modules/audit_checks.py).
  3. Запускает PSI на ключевых URL (mobile + desktop).
  4. Сохраняет отчёт в data/audits/YYYY-MM-DD/.
  5. Сравнивает с предыдущим аудитом → новые проблемы → Telegram-дельта.

Запуск:
    python3 -m seo-agent.modules.m2_audit            # полный прогон
    python3 -m seo-agent.modules.m2_audit --dry-run  # без Telegram

ENV:
    PSI_API_KEY                — обязательно для PSI-блока
    TELEGRAM_BOT_TOKEN, _CHAT_ID — для дельты
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Optional

# Делаем модуль запускаемым и из seo-agent/, и из корня репо
THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(THIS_DIR.parent))

from modules.crawler import fetch_sitemap_urls, crawl_all  # noqa: E402
from modules.audit_checks import (  # noqa: E402
    Issue,
    run_page_checks,
    check_duplicates,
    check_redirects,
)
from modules.pagespeed import psi_run, psi_summary  # noqa: E402

log = logging.getLogger(__name__)

SITE_ROOT = os.environ.get("M2_SITE_ROOT", "https://example.com").rstrip("/")
SITEMAP_URL = f"{SITE_ROOT}/sitemap.xml"
DATA_DIR = THIS_DIR.parent / "data" / "audits"

# URL, по которым гоняем PSI. Не весь сайт — лимит API + время.
PSI_KEY_URLS = [
    f"{SITE_ROOT}/",
    f"{SITE_ROOT}/doshkolnoe",
    f"{SITE_ROOT}/nachalnoe",
    f"{SITE_ROOT}/dopolnitelnoe",
    f"{SITE_ROOT}/spetsialnosti",
    f"{SITE_ROOT}/blog",
    f"{SITE_ROOT}/faq",
    f"{SITE_ROOT}/postuplenie",
    f"{SITE_ROOT}/kontakty",
]

# Пороги Core Web Vitals (Google)
THRESHOLDS = {
    "lcp_ms": 2500,   # выше — проблема
    "inp_ms": 200,
    "cls": 0.1,
    "tbt_ms": 200,
}


# ───── HTML-фаза ────────────────────────────────────────────────────

def run_html_audit() -> tuple[list[Issue], list]:
    """Краулинг + все per-page и global проверки. Возвращает (issues, crawl_results)."""
    log.info("Качаю sitemap %s", SITEMAP_URL)
    urls = fetch_sitemap_urls(SITEMAP_URL)
    log.info("Найдено URL: %d", len(urls))

    log.info("Краулю %d страниц...", len(urls))
    crawl_results = crawl_all(urls, delay=0.2)

    issues: list[Issue] = []
    for cr in crawl_results:
        if cr.error:
            issues.append(Issue(cr.url, "network_error", "high", cr.error))
            continue
        if cr.status >= 500:
            issues.append(Issue(cr.url, "http_5xx", "critical", f"HTTP {cr.status}"))
            continue
        if cr.status >= 400:
            issues.append(Issue(cr.url, "http_4xx", "high", f"HTTP {cr.status}"))
            continue
        if cr.html:
            issues.extend(run_page_checks(cr.url, cr.html))
        issues.extend(check_redirects(cr.url, cr.final_url, cr.redirects))

    # Глобальные проверки
    issues.extend(check_duplicates(crawl_results))
    return issues, crawl_results


# ───── PSI-фаза ─────────────────────────────────────────────────────

def run_psi_audit(urls: list[str]) -> dict[str, dict[str, dict]]:
    """PSI mobile+desktop по списку URL. Возвращает {url: {strategy: summary_dict}}."""
    if not os.environ.get("PSI_API_KEY"):
        log.warning("PSI_API_KEY не задан — пропускаю PSI-аудит")
        return {}

    psi_results: dict[str, dict[str, dict]] = {}
    for url in urls:
        psi_results[url] = {}
        for strategy in ("mobile", "desktop"):
            try:
                data = psi_run(url, strategy=strategy)
                psi_results[url][strategy] = psi_summary(data)
                s = psi_results[url][strategy]
                log.info(
                    "PSI %s %s: LCP=%.0fms perf=%s",
                    strategy, url, s.get("lcp_ms") or 0, s.get("performance"),
                )
            except Exception as e:
                log.warning("PSI %s %s failed: %s", strategy, url, e)
                psi_results[url][strategy] = {"error": str(e)}
    return psi_results


def psi_to_issues(psi_results: dict[str, dict[str, dict]]) -> list[Issue]:
    """Превратить плохие метрики PSI в Issue-записи (только mobile — у Яндекса mobile-first)."""
    issues: list[Issue] = []
    for url, by_strategy in psi_results.items():
        mobile = by_strategy.get("mobile", {})
        if mobile.get("error"):
            continue

        lcp = mobile.get("lcp_ms")
        if lcp and lcp > THRESHOLDS["lcp_ms"]:
            sev = "critical" if lcp > 4000 else "high"
            issues.append(Issue(url, "psi_lcp_slow_mobile", sev,
                                f"LCP mobile={lcp:.0f}ms (порог {THRESHOLDS['lcp_ms']}ms)"))

        inp = mobile.get("inp_ms")
        if inp and inp > THRESHOLDS["inp_ms"]:
            issues.append(Issue(url, "psi_inp_slow_mobile", "high",
                                f"INP mobile={inp:.0f}ms (порог {THRESHOLDS['inp_ms']}ms)"))

        cls = mobile.get("cls")
        if cls is not None and cls > THRESHOLDS["cls"]:
            issues.append(Issue(url, "psi_cls_bad_mobile", "high",
                                f"CLS mobile={cls:.3f} (порог {THRESHOLDS['cls']})"))

        perf = mobile.get("performance")
        if perf is not None and perf < 50:
            issues.append(Issue(url, "psi_performance_low_mobile", "high",
                                f"Performance mobile={perf} (плохо <50)"))
    return issues


# ───── Сохранение / дельта ──────────────────────────────────────────

def save_audit(date: str, issues: list[Issue], psi_results: dict, crawl_stats: dict) -> Path:
    audit_dir = DATA_DIR / date
    audit_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "date": date,
        "site": SITE_ROOT,
        "crawl_stats": crawl_stats,
        "issues": [i.to_dict() for i in issues],
        "psi": psi_results,
    }
    json_path = audit_dir / "audit.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Аудит сохранён: %s", json_path)

    md_path = audit_dir / "report.md"
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    log.info("Markdown-отчёт: %s", md_path)

    return audit_dir


def load_previous_issues(before_date: str) -> Optional[list[Issue]]:
    """Найти предыдущий аудит (любая папка YYYY-MM-DD < before_date) и вернуть его issues."""
    if not DATA_DIR.exists():
        return None
    candidates = sorted(
        [d for d in DATA_DIR.iterdir() if d.is_dir() and d.name < before_date and (d / "audit.json").exists()],
        reverse=True,
    )
    if not candidates:
        return None
    prev = json.loads((candidates[0] / "audit.json").read_text(encoding="utf-8"))
    log.info("Сравниваю с предыдущим аудитом: %s", candidates[0].name)
    return [Issue(**i) for i in prev.get("issues", [])]


def compute_delta(current: list[Issue], previous: Optional[list[Issue]]) -> tuple[list[Issue], list[Issue]]:
    """Возвращает (новые, исправленные)."""
    if previous is None:
        return (current, [])
    prev_keys = {i.key() for i in previous}
    curr_keys = {i.key() for i in current}
    new = [i for i in current if i.key() not in prev_keys]
    fixed = [i for i in previous if i.key() not in curr_keys]
    return (new, fixed)


# ───── Рендеринг ────────────────────────────────────────────────────

def render_markdown(payload: dict) -> str:
    issues = [Issue(**i) for i in payload["issues"]]
    by_sev: dict[str, list[Issue]] = {"critical": [], "high": [], "medium": [], "low": []}
    for i in issues:
        by_sev.setdefault(i.severity, []).append(i)

    lines: list[str] = []
    _site = (payload.get("site") or SITE_ROOT).replace("https://", "").replace("http://", "").strip("/")
    lines.append(f"# Tech-audit {_site} — {payload['date']}\n")
    stats = payload["crawl_stats"]
    lines.append(f"**Краулинг:** {stats['total']} страниц · "
                 f"2xx {stats['ok']} · 3xx {stats['redirect']} · "
                 f"4xx {stats['client_err']} · 5xx {stats['server_err']} · "
                 f"network errors {stats['network_err']}\n")
    lines.append(f"**Issues:** critical {len(by_sev['critical'])} · "
                 f"high {len(by_sev['high'])} · medium {len(by_sev['medium'])} · "
                 f"low {len(by_sev['low'])}\n")

    for sev in ("critical", "high", "medium", "low"):
        items = by_sev[sev]
        if not items:
            continue
        lines.append(f"\n## {sev.upper()} ({len(items)})\n")
        # Группируем по check
        by_check = Counter(i.check for i in items)
        for check, count in by_check.most_common():
            lines.append(f"\n### {check} ({count})\n")
            for i in items:
                if i.check == check:
                    lines.append(f"- `{i.url}` — {i.detail}")

    # PSI summary
    if payload.get("psi"):
        lines.append("\n## PageSpeed Insights\n")
        lines.append("\n| URL | Strategy | LCP | INP | CLS | Perf |")
        lines.append("|---|---|---|---|---|---|")
        for url, by_strategy in payload["psi"].items():
            for strategy, s in by_strategy.items():
                if s.get("error"):
                    lines.append(f"| `{url}` | {strategy} | error | | | |")
                    continue
                lcp = f"{s.get('lcp_ms', 0)/1000:.2f}s" if s.get("lcp_ms") else "—"
                inp = f"{s.get('inp_ms', 0):.0f}ms" if s.get("inp_ms") else "—"
                cls = f"{s.get('cls', 0):.3f}" if s.get("cls") is not None else "—"
                perf = s.get("performance", "—")
                lines.append(f"| `{url}` | {strategy} | {lcp} | {inp} | {cls} | {perf} |")

    return "\n".join(lines) + "\n"


def render_telegram_delta(new_issues: list[Issue], fixed_issues: list[Issue], date: str) -> str:
    """Короткое Telegram-сообщение по-человечески: что изменилось со вчера."""
    from notifiers.humanize import severity_label, check_ru, short_url, plural

    lines: list[str] = [f"🔍 Проверка сайта на технические ошибки · {date}"]

    if not new_issues and not fixed_issues:
        lines.append("\n✅ Всё стабильно — со вчерашнего дня ничего нового не сломалось.")
        return "\n".join(lines)

    if new_issues:
        by_sev = Counter(i.severity for i in new_issues)
        n = len(new_issues)
        lines.append(f"\n🔺 Появилось новых замечаний: {n}")
        # Счётчик по важности — только ненулевые уровни, понятными словами
        counts = [f"{severity_label(s)} — {by_sev[s]}"
                  for s in ("critical", "high", "medium", "low") if by_sev[s]]
        if counts:
            lines.append("   " + " · ".join(counts))

        important = [i for i in new_issues if i.severity in ("critical", "high")]
        if important:
            lines.append("\nЧто стоит посмотреть в первую очередь:")
            for i in important[:10]:
                lines.append(f"  • {check_ru(i.check)} — {short_url(i.url)}")
                if i.detail:
                    lines.append(f"      ({i.detail[:120]})")
            if len(important) > 10:
                hidden = len(important) - 10
                lines.append(f"  …и ещё {hidden} {plural(hidden, 'такое', 'таких', 'таких')}")

    if fixed_issues:
        m = len(fixed_issues)
        lines.append(f"\n✅ Исправлено за день: {m} {plural(m, 'замечание', 'замечания', 'замечаний')}")
        for i in fixed_issues[:5]:
            lines.append(f"  • {check_ru(i.check)} — {short_url(i.url)}")

    lines.append("\nПодробный разбор по каждой странице — в файле отчёта seo-agent/reports.")
    return "\n".join(lines)


# ───── Main ─────────────────────────────────────────────────────────

def crawl_stats(crawl_results: list) -> dict:
    stats = {"total": len(crawl_results), "ok": 0, "redirect": 0,
             "client_err": 0, "server_err": 0, "network_err": 0}
    for cr in crawl_results:
        if cr.error:
            stats["network_err"] += 1
        elif 200 <= cr.status < 300:
            stats["ok"] += 1
        elif 300 <= cr.status < 400:
            stats["redirect"] += 1
        elif 400 <= cr.status < 500:
            stats["client_err"] += 1
        elif cr.status >= 500:
            stats["server_err"] += 1
    return stats


def run_audit(dry_run: bool = False, skip_psi: bool = False) -> Path:
    today = dt.date.today().isoformat()
    log.info("=== M2 tech audit · %s ===", today)

    html_issues, crawl_results = run_html_audit()
    stats = crawl_stats(crawl_results)

    psi_results: dict = {}
    if not skip_psi:
        log.info("Запускаю PSI на %d ключевых URL...", len(PSI_KEY_URLS))
        psi_results = run_psi_audit(PSI_KEY_URLS)
        html_issues.extend(psi_to_issues(psi_results))

    audit_dir = save_audit(today, html_issues, psi_results, stats)

    previous = load_previous_issues(before_date=today)
    new_issues, fixed_issues = compute_delta(html_issues, previous)

    log.info("Итого issues: %d (новых: %d, исправлено с прошлого раза: %d)",
             len(html_issues), len(new_issues), len(fixed_issues))

    if dry_run:
        log.info("--dry-run: Telegram пропускаем")
    elif os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        from notifiers.telegram import send_telegram
        send_telegram(render_telegram_delta(new_issues, fixed_issues, today))
        log.info("Дельта отправлена в Telegram")
    else:
        log.info("TELEGRAM_BOT_TOKEN не задан — Telegram пропускаем")

    return audit_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="M2 tech audit для example.com")
    parser.add_argument("--dry-run", action="store_true", help="Не слать в Telegram")
    parser.add_argument("--skip-psi", action="store_true", help="Пропустить PSI-фазу (быстро для отладки)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_audit(dry_run=args.dry_run, skip_psi=args.skip_psi)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
