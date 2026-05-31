"""
Weekly digest — единый отчёт по понедельникам.

Собирает в одно сообщение:
  - M1: новые темы за неделю (по записям в semantic/*.json).
  - M2: последний tech-audit (severity-counts + delta vs прошлая неделя).
  - M3: топ-движения позиций за неделю (риз/фолы, новые ТОП-10).
  - GSC: общая динамика трафика (клики/показы текущая vs прошлая неделя).
  - Я.Вебмастер: ИКС, индекс.
  - Content-factory: статьи, опубликованные за неделю.

Запуск:
    python3 -m modules.weekly_digest             # production
    python3 -m modules.weekly_digest --dry-run   # без Telegram
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(THIS_DIR.parent))

log = logging.getLogger(__name__)

REPO_ROOT = THIS_DIR.parent.parent
REPORTS_DIR = THIS_DIR.parent / "reports"
AUDITS_DIR = THIS_DIR.parent / "data" / "audits"
RANKINGS_DIR = THIS_DIR.parent / "data" / "rankings"
SEMANTIC_DIR = THIS_DIR.parent / "data" / "semantic"
USED_CSV = REPO_ROOT / "content-factory" / "data" / "topics_used.csv"


def site_domain() -> str:
    """Человеческий домен сайта из env (example.com, example.com, ...)."""
    explicit = os.environ.get("SEO_SITE_DOMAIN", "").strip()
    if explicit:
        return explicit
    raw = os.environ.get("GSC_SITE_URL", "sc-domain:example.com").strip()
    return raw.replace("sc-domain:", "").replace("https://", "").replace("http://", "").strip("/")


# ───── Источники ────────────────────────────────────────────────────

def latest_audit() -> Optional[dict]:
    if not AUDITS_DIR.exists():
        return None
    days = sorted([d for d in AUDITS_DIR.iterdir() if d.is_dir()], reverse=True)
    for d in days:
        j = d / "audit.json"
        if j.exists():
            return json.loads(j.read_text(encoding="utf-8"))
    return None


def audit_delta(current: dict, week_ago: dt.date) -> tuple[int, int]:
    """Сравнить с аудитом неделю назад. Возвращает (новых, исправлено)."""
    if not AUDITS_DIR.exists():
        return (0, 0)
    cutoff = week_ago.isoformat()
    candidates = sorted([d for d in AUDITS_DIR.iterdir() if d.is_dir() and d.name <= cutoff], reverse=True)
    for d in candidates:
        j = d / "audit.json"
        if not j.exists():
            continue
        prev = json.loads(j.read_text(encoding="utf-8"))
        prev_keys = {(i["url"], i["check"], i["detail"]) for i in prev.get("issues", [])}
        curr_keys = {(i["url"], i["check"], i["detail"]) for i in current.get("issues", [])}
        return (len(curr_keys - prev_keys), len(prev_keys - curr_keys))
    return (0, 0)


def rankings_changes(week_ago: dt.date) -> dict:
    """Сравнить последний rankings-снимок с тем, что был ~7 дней назад."""
    if not RANKINGS_DIR.exists():
        return {}
    snapshots = sorted([p for p in RANKINGS_DIR.glob("*.csv")], reverse=True)
    if not snapshots:
        return {}
    latest = snapshots[0]
    # Ищем снимок не новее week_ago
    cutoff = week_ago.isoformat()
    older = next((p for p in snapshots[1:] if p.stem <= cutoff), None)

    def load(path: Path) -> dict[str, dict]:
        out = {}
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["keyword"]] = row
        return out

    curr = load(latest)
    in_top10 = sum(1 for r in curr.values()
                   if r.get("google_pos") and float(r["google_pos"]) <= 10)
    total = len(curr)

    result = {
        "date": latest.stem,
        "total": total,
        "google_top10": in_top10,
        "google_top30": sum(1 for r in curr.values()
                            if r.get("google_pos") and float(r["google_pos"]) <= 30),
        "yandex_top10": sum(1 for r in curr.values()
                            if r.get("yandex_pos") and float(r["yandex_pos"]) <= 10),
        "risers": [],
        "fallers": [],
        "new_top10": [],
    }

    if not older:
        return result

    prev = load(older)
    for kw, row in curr.items():
        if kw not in prev:
            continue
        for engine in ("google", "yandex"):
            try:
                new = float(row[f"{engine}_pos"]) if row.get(f"{engine}_pos") else None
                old = float(prev[kw][f"{engine}_pos"]) if prev[kw].get(f"{engine}_pos") else None
            except (KeyError, ValueError):
                continue
            if new is None or old is None:
                continue
            diff = new - old
            if diff <= -5:
                result["risers"].append((kw, old, new, engine))
            elif diff >= 5:
                result["fallers"].append((kw, old, new, engine))
            if old > 10 and new <= 10:
                result["new_top10"].append((kw, old, new, engine))

    result["risers"].sort(key=lambda x: x[1] - x[2], reverse=True)
    result["fallers"].sort(key=lambda x: x[2] - x[1], reverse=True)
    return result


def semantic_summary(week_ago: dt.date) -> dict:
    """Подсчитать темы, добавленные в backlog за неделю."""
    if not SEMANTIC_DIR.exists():
        return {"added_total": 0, "snapshots": 0, "topics_examples": []}
    cutoff = week_ago.isoformat()
    snapshots = [p for p in SEMANTIC_DIR.glob("*.json") if p.stem >= cutoff]
    added_total = 0
    topics: list[dict] = []
    for s in sorted(snapshots):
        data = json.loads(s.read_text(encoding="utf-8"))
        added_total += data.get("topics_added", 0)
        topics.extend(data.get("topics", []))
    return {
        "added_total": added_total,
        "snapshots": len(snapshots),
        "topics_examples": topics[:5],
    }


def content_factory_published(week_ago: dt.date) -> list[dict]:
    """Статьи, опубликованные за неделю (из topics_used.csv)."""
    if not USED_CSV.exists():
        return []
    cutoff = week_ago.isoformat()
    out: list[dict] = []
    with USED_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("date") or "") >= cutoff and row.get("status") == "published":
                out.append(row)
    return out


def yandex_webmaster_summary() -> dict:
    """ИКС и количество страниц в индексе."""
    try:
        from modules.yandex_webmaster import yw_get_user_id, yw_list_hosts, yw_host_info
        user_id = yw_get_user_id()
        hosts = yw_list_hosts(user_id)
        domain = site_domain()
        for h in hosts:
            url = h.get("unicode_host_url", "")
            if domain in url and url.startswith("https"):
                info = yw_host_info(user_id, h["host_id"])
                return {
                    "sqi": info.get("sqi"),
                    "verified": info.get("verified"),
                    "main_mirror": info.get("main_mirror_host_id"),
                }
    except Exception as e:
        log.warning("Я.Вебмастер: %s", e)
    return {}


def gsc_traffic_summary() -> dict:
    """Клики и показы за последние 7 дней + за предыдущие 7."""
    try:
        from modules.gsc_client import gsc_service, query_search_analytics
        svc = gsc_service()
        site_url = os.environ.get("GSC_SITE_URL", "sc-domain:example.com")

        end_curr = (dt.date.today() - dt.timedelta(days=2)).isoformat()
        start_curr = (dt.date.today() - dt.timedelta(days=9)).isoformat()
        end_prev = (dt.date.today() - dt.timedelta(days=10)).isoformat()
        start_prev = (dt.date.today() - dt.timedelta(days=17)).isoformat()

        def totals(start, end):
            rows = query_search_analytics(svc, site_url, start, end, [], row_limit=1)
            if rows:
                r = rows[0]
                return int(r.get("clicks", 0)), int(r.get("impressions", 0))
            return 0, 0

        c1, i1 = totals(start_curr, end_curr)
        c2, i2 = totals(start_prev, end_prev)
        return {
            "clicks_week": c1, "impressions_week": i1,
            "clicks_prev": c2, "impressions_prev": i2,
            "delta_clicks": c1 - c2, "delta_impressions": i1 - i2,
        }
    except Exception as e:
        log.warning("GSC traffic: %s", e)
    return {}


# ───── Рендер ───────────────────────────────────────────────────────

def render_digest(today: dt.date, payload: dict) -> str:
    week_ago = today - dt.timedelta(days=7)
    lines: list[str] = []
    lines.append(f"# Как прошла неделя по продвижению сайта · {site_domain()}\n")
    lines.append(f"Период: с {week_ago.isoformat()} по {today.isoformat()}.\n")
    lines.append("_Короткий отчёт о том, как сайт растёт в Яндексе и Google. "
                 "Все цифры — простыми словами._\n")

    # ── Трафик
    t = payload.get("traffic", {})
    if t:
        dc, di = t["delta_clicks"], t["delta_impressions"]
        lines.append("## Сколько людей пришло из поиска")
        lines.append(f"- Перешли на сайт из Google: **{t['clicks_week']}** "
                     f"(на {dc:+d} к прошлой неделе)")
        lines.append(f"- Сайт показался в результатах поиска: **{t['impressions_week']}** раз "
                     f"(на {di:+d})")
        lines.append("\n> «Показы» — сколько раз сайт мелькнул в поиске. «Переходы» — "
                     "сколько человек реально кликнули и зашли.")

    # ── Индекс
    yw = payload.get("yandex_webmaster", {})
    if yw.get("sqi") is not None:
        lines.append("\n## Оценка сайта Яндексом")
        lines.append(f"- Индекс качества сайта (ИКС) в Яндексе: **{yw['sqi']}**")
        lines.append("\n> ИКС — общая оценка сайта от Яндекса (от 0 и выше). "
                     "Чем больше, тем больше Яндекс доверяет сайту. Растёт медленно, это нормально.")

    # ── Позиции
    r = payload.get("rankings", {})
    if r:
        lines.append("\n## Места сайта в поиске")
        lines.append(f"- Всего отслеживаем запросов: {r.get('total', 0)}")
        lines.append(f"- На первой странице Google (ТОП-10): **{r.get('google_top10', 0)}**, "
                     f"в первых тридцати (ТОП-30): {r.get('google_top30', 0)}")
        if r.get("yandex_top10"):
            lines.append(f"- На первой странице Яндекса (ТОП-10): **{r['yandex_top10']}**")

        def eng_ru(eng):
            return "Яндекс" if eng == "yandex" else "Google"

        if r.get("new_top10"):
            lines.append(f"\n**Вышли на первую страницу поиска ({len(r['new_top10'])}) — это успех:**")
            for kw, old, new, eng in r["new_top10"][:10]:
                lines.append(f"  - «{kw}»: было {old:.0f}-е → стало {new:.0f}-е место ({eng_ru(eng)})")
        if r.get("risers"):
            lines.append(f"\n**Заметно выросли ({len(r['risers'])}):**")
            for kw, old, new, eng in r["risers"][:5]:
                lines.append(f"  - «{kw}»: было {old:.0f}-е → стало {new:.0f}-е место ({eng_ru(eng)})")
        if r.get("fallers"):
            lines.append(f"\n**Заметно просели ({len(r['fallers'])}) — стоит присмотреться:**")
            for kw, old, new, eng in r["fallers"][:5]:
                lines.append(f"  - «{kw}»: было {old:.0f}-е → стало {new:.0f}-е место ({eng_ru(eng)})")
        lines.append("\n> Чем меньше номер места, тем выше сайт в поиске. "
                     "Первое место — самый верх выдачи.")

    # ── Тех. аудит
    a = payload.get("audit")
    if a:
        issues = a.get("issues", [])
        by_sev = {}
        for i in issues:
            by_sev[i["severity"]] = by_sev.get(i["severity"], 0) + 1
        new_count, fixed_count = payload.get("audit_delta", (0, 0))
        urgent = by_sev.get("critical", 0) + by_sev.get("high", 0)
        minor = by_sev.get("medium", 0) + by_sev.get("low", 0)
        lines.append("\n## Технические замечания по сайту")
        lines.append(f"- Важных (мешают продвижению): **{urgent}**")
        lines.append(f"- Мелких (желательно поправить): {minor}")
        lines.append(f"- За неделю: появилось {new_count}, исправлено {fixed_count}")
        stats = a.get("crawl_stats", {})
        if stats and (stats.get("client_err") or stats.get("server_err")):
            lines.append(f"- Битых/недоступных страниц: "
                         f"{stats.get('client_err', 0) + stats.get('server_err', 0)} "
                         f"из {stats.get('total')}")

    # ── Новые темы
    s = payload.get("semantic", {})
    if s:
        lines.append("\n## Новые темы для статей")
        lines.append(f"- Придумали новых тем за неделю: **{s.get('added_total', 0)}** "
                     "(пойдут в очередь на автопубликацию)")
        if s.get("topics_examples"):
            lines.append("\n**Примеры тем:**")
            for t in s["topics_examples"][:5]:
                lines.append(f"  - {t.get('topic')}")

    # ── Опубликовано
    cf = payload.get("content_published", [])
    if cf:
        lines.append(f"\n## Опубликовали статей за неделю: {len(cf)}")
        for row in cf[:10]:
            lines.append(f"  - {row.get('date')}: [{row.get('title')}]({row.get('url')})")

    lines.append("\n---\n_Отчёт собран автоматически. Данные — из Яндекс.Вебмастера и "
                 "Google Search Console (официальные кабинеты вебмастера)._")
    return "\n".join(lines)


def render_telegram(today: dt.date, payload: dict) -> str:
    """Короткая версия для Telegram — итоги недели человеческим языком."""
    from notifiers.humanize import severity_label, plural

    def eng_ru(eng):
        return "Яндекс" if eng == "yandex" else "Google"

    lines = [f"📈 Итоги недели по продвижению сайта · {today.isoformat()}"]

    t = payload.get("traffic", {})
    if t:
        dc, di = t["delta_clicks"], t["delta_impressions"]
        trend = "это больше, чем неделей раньше" if dc > 0 else (
            "это меньше, чем неделей раньше" if dc < 0 else "столько же, сколько неделей раньше")
        lines.append(f"\n👥 Из поиска Google пришло {t['clicks_week']} "
                     f"{plural(t['clicks_week'], 'переход', 'перехода', 'переходов')} на сайт "
                     f"({dc:+d}) — {trend}.")
        lines.append(f"   Сайт показался в поиске {t['impressions_week']} раз ({di:+d}).")

    r = payload.get("rankings", {})
    if r:
        lines.append(f"\n📊 В первой странице Google (ТОП-10): {r.get('google_top10', 0)} "
                     f"из {r.get('total', 0)} запросов, в ТОП-30: {r.get('google_top30', 0)}.")
        if r.get("new_top10"):
            lines.append(f"   🟢 Вышли на первую страницу: {len(r['new_top10'])} "
                         f"{plural(len(r['new_top10']), 'запрос', 'запроса', 'запросов')}")
            for kw, old, new, eng in r["new_top10"][:3]:
                lines.append(f"      • «{kw}» (было {old:.0f}-е → стало {new:.0f}-е, {eng_ru(eng)})")
        if r.get("fallers"):
            lines.append(f"   🔻 Заметно просели: {len(r['fallers'])}")
            for kw, old, new, eng in r["fallers"][:3]:
                lines.append(f"      • «{kw}» (было {old:.0f}-е → стало {new:.0f}-е, {eng_ru(eng)})")

    a = payload.get("audit")
    if a:
        new_count, fixed_count = payload.get("audit_delta", (0, 0))
        by_sev = {}
        for i in a.get("issues", []):
            by_sev[i["severity"]] = by_sev.get(i["severity"], 0) + 1
        urgent = by_sev.get("critical", 0) + by_sev.get("high", 0)
        lines.append(f"\n🔍 Технических замечаний на сайте: важных — {urgent}, "
                     f"помельче — {by_sev.get('medium', 0) + by_sev.get('low', 0)}.")
        lines.append(f"   За неделю: появилось {new_count}, исправлено {fixed_count}.")

    s = payload.get("semantic", {})
    if s and s.get("added_total"):
        n = s["added_total"]
        lines.append(f"\n📚 Придумали {n} новых {plural(n, 'тему', 'темы', 'тем')} для статей "
                     f"— они в очереди на публикацию.")

    cf = payload.get("content_published", [])
    if cf:
        n = len(cf)
        lines.append(f"\n✍️ За неделю опубликовали {n} {plural(n, 'статью', 'статьи', 'статей')}.")

    lines.append("\nПодробный отчёт со всеми ссылками — в файле seo-agent/reports/weekly-*.md")
    return "\n".join(lines)


# ───── Main ─────────────────────────────────────────────────────────

def run_digest(dry_run: bool = False) -> Path:
    today = dt.date.today()
    week_ago = today - dt.timedelta(days=7)
    log.info("=== Weekly digest · %s ===", today)

    payload = {
        "audit": latest_audit(),
        "rankings": rankings_changes(week_ago),
        "semantic": semantic_summary(week_ago),
        "content_published": content_factory_published(week_ago),
        "yandex_webmaster": yandex_webmaster_summary(),
        "traffic": gsc_traffic_summary(),
    }
    audit = payload.get("audit")
    if audit:
        payload["audit_delta"] = audit_delta(audit, week_ago)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    iso_year, iso_week, _ = today.isocalendar()
    report_path = REPORTS_DIR / f"weekly-{iso_year}-W{iso_week:02d}.md"
    report_path.write_text(render_digest(today, payload), encoding="utf-8")
    log.info("Markdown digest: %s", report_path)

    if not dry_run and os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        from notifiers.telegram import send_telegram
        send_telegram(render_telegram(today, payload))
        log.info("Digest отправлен в Telegram")

    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly SEO digest")
    parser.add_argument("--dry-run", action="store_true", help="Не слать в Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_digest(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
