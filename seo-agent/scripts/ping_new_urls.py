"""
Пинг свежих URL после публикации статьи / лендинга.

Делает три вещи параллельно (каждая независима — падение одной не ломает остальные):
  1. IndexNow → Яндекс + Bing (по списку URL).
  2. Я.Вебмастер recrawl → ставит URL в очередь переобхода (точечно, по квоте).
  3. GSC sitemaps.submit → пинг Google переобработать sitemap.xml.

Использование:
    # Передать список URL руками
    python scripts/ping_new_urls.py \\
        https://example.com/blog/zarplata-vospitatelya-2026 \\
        https://example.com/blog/spo-ili-vo-dlya-vospitatelya

    # Автодетект новых URL из git diff (используется в GHA seo-ping.yml)
    python scripts/ping_new_urls.py --from-git-diff

    # Только sitemap.submit, без точечных пингов
    python scripts/ping_new_urls.py --sitemap-only

    # Dry-run (ничего не отправляет, печатает план)
    python scripts/ping_new_urls.py --from-git-diff --dry-run

ENV:
    SITE_URL_BASE              — https://example.com (по умолчанию)
    SITEMAP_URL                — https://example.com/sitemap.xml (по умолчанию)
    GSC_SITE_URL               — sc-domain:example.com
    GSC_OAUTH_*                — для GSC sitemap.submit
    YANDEX_WEBMASTER_TOKEN     — для recrawl
    INDEXNOW_KEY               — опциональный override (дефолт зашит, совпадает с public/<key>.txt)
    TELEGRAM_BOT_TOKEN/CHAT_ID — опционально, для алёрта по итогу
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Чтобы импорты modules.* работали при запуске из любой директории.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.indexnow import ping_indexnow  # noqa: E402
from modules.gsc_sitemap import submit_sitemap  # noqa: E402
from modules import yandex_webmaster as yw  # noqa: E402

log = logging.getLogger("ping")

SITE_BASE = os.environ.get("SITE_URL_BASE", "https://example.com").rstrip("/")
SITEMAP_URL = os.environ.get("SITEMAP_URL", f"{SITE_BASE}/sitemap.xml")
GSC_SITE_URL = os.environ.get("GSC_SITE_URL", "sc-domain:example.com")
HOST_URL = os.environ.get("INDEXNOW_HOST", "example.com")

# Спец-маппинг файлов лендингов → URL (если slug ≠ URL-пути).
# Блог мапится автоматически ниже (content/blog/<slug>.mdx → /blog/<slug>).
# Добавь свои пары при необходимости, напр. "content/landing/about.mdx": "/about".
PROGRAM_TO_URL: dict[str, str] = {}


def collect_changed_files(base_ref: str = "HEAD~1", head_ref: str = "HEAD") -> list[str]:
    """Список изменённых файлов между двумя ref'ами через git diff --name-only.

    Возвращает пути относительно корня репо. Если git упал — пустой список.
    """
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", base_ref, head_ref],
            cwd=str(_repo_root()),
            text=True,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("git diff failed: %s — считаем, что новых файлов нет", e)
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _repo_root() -> Path:
    # ROOT = seo-agent/. Корень репо — на уровень выше.
    return ROOT.parent


def files_to_urls(files: list[str]) -> list[str]:
    """Смапить изменённые файлы → URL для пинга. Дедуп сохраняется."""
    urls: list[str] = []
    seen: set[str] = set()

    def add(u: str):
        if u not in seen:
            seen.add(u)
            urls.append(u)

    for f in files:
        f = f.strip()
        if not f:
            continue

        # Блог: content/blog/<slug>.mdx → /blog/<slug>
        if f.startswith("content/blog/") and f.endswith(".mdx"):
            slug = Path(f).stem
            add(f"{SITE_BASE}/blog/{slug}")
            continue

        # Флагманы.
        if f in PROGRAM_TO_URL:
            add(f"{SITE_BASE}{PROGRAM_TO_URL[f]}")
            continue

        # Каталог специальностей (один JSON на 33 специальности — без чтения diff
        # не понять, какие именно изменены; пингуем главный каталог).
        if f == "content/specialties.json":
            add(f"{SITE_BASE}/spetsialnosti")
            continue

        # Главная и сервисные страницы — по common.
        if f == "content/home.mdx":
            add(f"{SITE_BASE}/")
            continue
        if f == "content/pages.json":
            # Затрагивает много страниц — пингуем sitemap, без точечных URL.
            pass

    return urls


def run_indexnow(urls: list[str], dry_run: bool) -> dict:
    if not urls:
        return {"skipped": True, "reason": "no urls"}
    log.info("→ IndexNow: %d URL", len(urls))
    return ping_indexnow(urls, dry_run=dry_run)


def run_recrawl(urls: list[str], dry_run: bool) -> dict:
    """Поставить URL в очередь Я.Вебмастера. Учитывает квоту."""
    if not urls:
        return {"skipped": True, "reason": "no urls"}
    if not os.environ.get("YANDEX_WEBMASTER_TOKEN", "").strip():
        log.warning("YANDEX_WEBMASTER_TOKEN не задан — пропускаю recrawl")
        return {"skipped": True, "reason": "no token"}

    if dry_run:
        log.info("DRY-RUN recrawl: %d URL", len(urls))
        return {"dry_run": True, "planned": len(urls)}

    try:
        user_id, host_id = yw.yw_resolve_host_id(host_url=HOST_URL)
    except Exception as e:  # noqa: BLE001
        log.error("yw_resolve_host_id failed: %s", e)
        return {"error": f"resolve_host_id: {e}"}

    try:
        quota = yw.yw_recrawl_quota(user_id, host_id)
    except Exception as e:  # noqa: BLE001
        log.warning("yw_recrawl_quota failed (продолжаем без чека квоты): %s", e)
        quota = {}

    # Поле остатка в API называется quota_remainder (не *_remaining_daily).
    remaining = int(
        quota.get("quota_remainder")
        if quota.get("quota_remainder") is not None
        else (quota.get("quota_remaining_daily") or quota.get("daily_quota_remain") or 0)
    )
    daily = int(quota.get("daily_quota") or 0)
    have_quota_info = bool(quota)
    log.info("  recrawl quota: remaining=%s daily=%s", remaining, daily)

    if have_quota_info and remaining <= 0:
        log.warning("  recrawl: дневная квота исчерпана (remaining=0) — пропускаю все %d URL", len(urls))
        return {
            "user_id": user_id,
            "host_id": host_id,
            "quota_remaining_before": 0,
            "daily_quota": daily,
            "sent": [],
            "skipped_by_quota": urls,
        }

    sent: list[dict] = []
    skipped_by_quota: list[str] = []
    for i, url in enumerate(urls):
        # Лимитируем только если знаем остаток квоты.
        if have_quota_info and i >= remaining:
            skipped_by_quota.append(url)
            continue
        try:
            r = yw.yw_recrawl_url(user_id, host_id, url)
            sent.append({"url": url, "task_id": r.get("task_id"), "ok": True})
            log.info("  ✓ recrawl %s — task=%s", url, r.get("task_id"))
        except Exception as e:  # noqa: BLE001
            sent.append({"url": url, "ok": False, "error": str(e)})
            log.error("  ✗ recrawl %s: %s", url, e)

    return {
        "user_id": user_id,
        "host_id": host_id,
        "quota_remaining_before": remaining,
        "daily_quota": daily,
        "sent": sent,
        "skipped_by_quota": skipped_by_quota,
    }


def run_sitemap_submit(dry_run: bool) -> dict:
    log.info("→ GSC sitemap.submit %s", SITEMAP_URL)
    return submit_sitemap(GSC_SITE_URL, SITEMAP_URL, dry_run=dry_run)


def save_log(result: dict) -> Path:
    log_dir = ROOT / "data" / "ping-log"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = log_dir / f"{ts}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return path


def telegram_summary(result: dict) -> None:
    if not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        return
    try:
        from notifiers.telegram import send_telegram
    except Exception:  # noqa: BLE001
        return

    urls = result.get("urls", [])
    inx = result.get("indexnow", {})
    rcr = result.get("recrawl", {})
    sm = result.get("sitemap", {})

    lines = [f"🔔 SEO-ping · {len(urls)} URL"]
    if urls[:5]:
        lines.append("\n".join(f"• {u}" for u in urls[:5]))
        if len(urls) > 5:
            lines.append(f"… и ещё {len(urls) - 5}")

    if inx.get("skipped"):
        lines.append(f"\nIndexNow: пропуск ({inx.get('reason')})")
    else:
        lines.append(f"\nIndexNow: ok={inx.get('ok', 0)} failed={inx.get('failed', 0)} batches={inx.get('batches', 0)}")

    if rcr.get("skipped"):
        lines.append(f"Я.Вебмастер recrawl: пропуск ({rcr.get('reason')})")
    elif rcr.get("error"):
        lines.append(f"Я.Вебмастер recrawl: ошибка — {rcr['error']}")
    else:
        sent = rcr.get("sent", [])
        ok = sum(1 for s in sent if s.get("ok"))
        fail = len(sent) - ok
        skipped = len(rcr.get("skipped_by_quota", []))
        line = f"Я.Вебмастер recrawl: ok={ok} fail={fail}"
        if skipped:
            line += f" skipped_by_quota={skipped}"
        if rcr.get("quota_remaining_before") is not None:
            line += f" quota_before={rcr['quota_remaining_before']}"
        lines.append(line)

    if sm.get("ok"):
        lines.append("GSC sitemap.submit: ok")
    else:
        lines.append(f"GSC sitemap.submit: ошибка — {sm.get('error', 'unknown')}")

    try:
        send_telegram("\n".join(lines))
    except Exception as e:  # noqa: BLE001
        log.warning("telegram_summary failed: %s", e)


def main() -> int:
    parser = argparse.ArgumentParser(description="Пинг свежих URL после публикации")
    parser.add_argument("urls", nargs="*", help="URL для пинга (если не задан --from-git-diff)")
    parser.add_argument("--from-git-diff", action="store_true",
                        help="Вычислить URL из git diff HEAD~1..HEAD")
    parser.add_argument("--base-ref", default="HEAD~1", help="git base ref для diff")
    parser.add_argument("--head-ref", default="HEAD", help="git head ref для diff")
    parser.add_argument("--sitemap-only", action="store_true",
                        help="Только GSC sitemap.submit, без точечных пингов")
    parser.add_argument("--skip-indexnow", action="store_true")
    parser.add_argument("--skip-recrawl", action="store_true")
    parser.add_argument("--skip-sitemap", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    # Собираем целевые URL.
    if args.sitemap_only:
        urls: list[str] = []
        log.info("режим --sitemap-only: пингуем только sitemap")
    elif args.from_git_diff:
        changed = collect_changed_files(args.base_ref, args.head_ref)
        log.info("git diff: %d файлов изменено", len(changed))
        for f in changed[:20]:
            log.info("  %s", f)
        if len(changed) > 20:
            log.info("  … и ещё %d", len(changed) - 20)
        urls = files_to_urls(changed)
    else:
        urls = list(args.urls)

    if urls:
        log.info("→ Целевые URL (%d):", len(urls))
        for u in urls:
            log.info("    %s", u)
    elif not args.sitemap_only:
        log.warning("Список URL пуст — пингую только sitemap")

    result = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "urls": urls,
    }

    if urls and not args.skip_indexnow:
        result["indexnow"] = run_indexnow(urls, dry_run=args.dry_run)
    else:
        result["indexnow"] = {"skipped": True, "reason": "no urls" if not urls else "--skip-indexnow"}

    if urls and not args.skip_recrawl:
        result["recrawl"] = run_recrawl(urls, dry_run=args.dry_run)
    else:
        result["recrawl"] = {"skipped": True, "reason": "no urls" if not urls else "--skip-recrawl"}

    if not args.skip_sitemap:
        result["sitemap"] = run_sitemap_submit(dry_run=args.dry_run)
    else:
        result["sitemap"] = {"skipped": True, "reason": "--skip-sitemap"}

    result["finished_at"] = datetime.now(timezone.utc).isoformat()

    log_path = save_log(result)
    log.info("→ Лог сохранён: %s", log_path.relative_to(_repo_root()))

    if not args.dry_run:
        telegram_summary(result)

    # exit 0 даже при частичных ошибках — пинги это best-effort, GHA не валим.
    return 0


if __name__ == "__main__":
    sys.exit(main())
