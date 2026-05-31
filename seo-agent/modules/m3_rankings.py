"""
M3 — Rank tracker.

Что делает:
  1. Берёт unified-список целевых ключей (из programs/*.mdx, blog/*.mdx
     frontmatter `seoKeywords` + topics_backlog + topics_used).
  2. Для каждого ключа: получает среднюю позицию в Google за последние 7 дней
     (GSC searchanalytics).
  3. Если есть Topvisor API token — добавляет позиции в Яндексе и Google
     с конкретного дня (более точные данные, чем GSC averaging).
  4. Сохраняет в seo-agent/data/rankings/YYYY-MM-DD.csv.
  5. Сравнивает с предыдущим днём → алертит при падении ≥5 позиций или
     вылете из ТОП-30/10.

Запуск:
    python3 -m modules.m3_rankings              # ежедневный прогон
    python3 -m modules.m3_rankings --dry-run    # без Telegram
    python3 -m modules.m3_rankings --refresh-seeds  # пересобрать список ключей

ENV:
    GSC_OAUTH_REFRESH_TOKEN + CLIENT_ID + CLIENT_SECRET
    GSC_SITE_URL=sc-domain:example.com
    TOPVISOR_API_TOKEN, TOPVISOR_PROJECT_ID (опц.)
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(THIS_DIR.parent))

from modules.gsc_client import gsc_service, query_search_analytics  # noqa: E402

log = logging.getLogger(__name__)

REPO_ROOT = THIS_DIR.parent.parent
RANKINGS_DIR = THIS_DIR.parent / "data" / "rankings"
SEEDS_FILE = RANKINGS_DIR / "tracked-keywords.json"

# Алерт-пороги
RANK_DROP_THRESHOLD = 5  # позиций
LOST_TOP_10 = "top10"
LOST_TOP_30 = "top30"


# ───── Сбор seed-ключей ─────────────────────────────────────────────

def collect_seed_keywords() -> list[str]:
    """Собрать unified-список ключей из MDX-frontmatter и CSV-файлов content-factory."""
    keywords: set[str] = set()

    # 1. content/programs/*.mdx
    for p in sorted((REPO_ROOT / "content" / "programs").glob("*.mdx")):
        keywords.update(_extract_seokeywords_from_mdx(p))

    # 2. content/blog/*.mdx
    for p in sorted((REPO_ROOT / "content" / "blog").glob("*.mdx")):
        keywords.update(_extract_seokeywords_from_mdx(p))
        keywords.update(_extract_field_from_mdx(p, "primary_keyword"))

    # 3. content-factory CSVs
    for csv_path in [
        REPO_ROOT / "content-factory" / "data" / "topics_used.csv",
        REPO_ROOT / "content-factory" / "data" / "topics_backlog.csv",
    ]:
        if not csv_path.exists():
            continue
        with csv_path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                kw = (row.get("primary_keyword") or "").strip().lower()
                if kw:
                    keywords.add(kw)

    # Нормализация: lowercase, single spaces, без хвостовых знаков
    norm = {re.sub(r"\s+", " ", k.strip(" .,!?;:")).lower() for k in keywords if k}
    norm.discard("")
    log.info("Seed-keywords из MDX + CSV: %d", len(norm))
    return sorted(norm)


def _extract_seokeywords_from_mdx(path: Path) -> list[str]:
    """Достать массив seoKeywords из frontmatter MDX."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return []
    # Берём блок между --- и ---
    end = text.find("\n---", 4)
    if end == -1:
        return []
    fm = text[4:end]
    # Простой YAML-парс для массива `seoKeywords:\n  - "..."` и через запятые
    out: list[str] = []
    in_block = False
    for line in fm.splitlines():
        if line.startswith("seoKeywords:"):
            in_block = True
            # Inline `seoKeywords: ["foo", "bar"]`
            rest = line[len("seoKeywords:"):].strip()
            if rest.startswith("["):
                try:
                    out.extend(json.loads(rest))
                except Exception:
                    pass
                in_block = False
            continue
        if in_block:
            m = re.match(r'^\s*-\s+["\']?([^"\']+?)["\']?\s*$', line)
            if m:
                out.append(m.group(1))
            elif not line.startswith(" ") and not line.startswith("\t"):
                in_block = False
    return [k.strip().lower() for k in out if k.strip()]


def _extract_field_from_mdx(path: Path, field: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    m = re.search(rf'^{field}:\s*["\']?([^"\'\n]+?)["\']?\s*$', text, flags=re.MULTILINE)
    return [m.group(1).strip().lower()] if m else []


def save_seeds(seeds: list[str]) -> Path:
    RANKINGS_DIR.mkdir(parents=True, exist_ok=True)
    SEEDS_FILE.write_text(
        json.dumps({"updated": dt.date.today().isoformat(), "keywords": seeds},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return SEEDS_FILE


def load_seeds() -> list[str]:
    if not SEEDS_FILE.exists():
        return []
    return json.loads(SEEDS_FILE.read_text(encoding="utf-8")).get("keywords", [])


# ───── GSC: средние позиции ────────────────────────────────────────

@dataclass
class RankRow:
    keyword: str
    google_pos: Optional[float] = None
    google_clicks: int = 0
    google_impressions: int = 0
    yandex_pos: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "keyword": self.keyword,
            "google_pos": round(self.google_pos, 1) if self.google_pos else None,
            "google_clicks": self.google_clicks,
            "google_impressions": self.google_impressions,
            "yandex_pos": round(self.yandex_pos, 1) if self.yandex_pos else None,
        }


def fetch_gsc_positions(seeds: list[str], days: int = 7) -> dict[str, RankRow]:
    """Получить средние позиции в Google за последние N дней (с lag 2 дня)."""
    end_date = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    start_date = (dt.date.today() - dt.timedelta(days=days + 2)).isoformat()
    site_url = os.environ.get("GSC_SITE_URL", "sc-domain:example.com")

    log.info("GSC: позиции %s → %s, site=%s", start_date, end_date, site_url)
    svc = gsc_service()
    rows = query_search_analytics(svc, site_url, start_date, end_date, ["query"], row_limit=5000)

    # Индекс по нормализованному query
    seed_set = {s.lower() for s in seeds}
    result: dict[str, RankRow] = {}
    for s in seeds:
        result[s] = RankRow(keyword=s)

    matched = 0
    for row in rows:
        q = row["keys"][0].lower().strip()
        if q in seed_set:
            result[q].google_pos = float(row.get("position", 0))
            result[q].google_clicks = int(row.get("clicks", 0))
            result[q].google_impressions = int(row.get("impressions", 0))
            matched += 1
    log.info("GSC: %d/%d seed-ключей с показами в Google", matched, len(seeds))
    return result


# ───── Topvisor: позиции в Яндексе (если доступен) ─────────────────

TOPVISOR_API = "https://api.topvisor.com/v2/json"


def fetch_topvisor_positions(seeds: list[str]) -> dict[str, float]:
    """Получить позиции в Яндексе через Topvisor.
    Документация: https://topvisor.com/api/

    Возвращает {keyword: yandex_position}. Требует:
      - TOPVISOR_API_TOKEN — токен из ЛК Topvisor
      - TOPVISOR_PROJECT_ID — id проекта с уже добавленными ключами
    """
    token = os.environ.get("TOPVISOR_API_TOKEN", "").strip()
    project_id = os.environ.get("TOPVISOR_PROJECT_ID", "").strip()
    if not token or not project_id:
        log.info("Topvisor: токен/project_id не заданы — пропускаю Яндекс-позиции")
        return {}

    # API Topvisor: positions_2/history даёт исторические позиции.
    # Для одного дневного снимка проще использовать positions_2/summary.
    # Документация: https://topvisor.com/api/services/positions/get-history/
    headers = {
        "Content-Type": "application/json",
        "User-Id": project_id.split("-")[0] if "-" in project_id else token[:8],
        "Authorization": f"Bearer {token}",
    }
    today = dt.date.today().isoformat()
    payload = {
        "project_id": int(project_id),
        "regions_indexes": [0],  # 0 = Россия (вся РФ) — настроить в ЛК Topvisor
        "dates": [today, today],
        "show_headers": 1,
        "show_visitors": 0,
    }
    try:
        response = requests.post(
            f"{TOPVISOR_API}/get/positions_2/history",
            json=payload, headers=headers, timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        log.warning("Topvisor: запрос упал — %s", e)
        return {}

    result: dict[str, float] = {}
    # Структура ответа Topvisor: { result: { headers, keywords: [...] } } или { result: null }
    try:
        result_root = data.get("result") or {}
        keywords = result_root.get("keywords") if isinstance(result_root, dict) else []
        keywords = keywords or []
        for kw in keywords:
            if not isinstance(kw, dict):
                continue
            text = (kw.get("name") or "").lower().strip()
            positions_data = kw.get("positionsData") or {}
            for pos_key, pos in positions_data.items():
                if not pos or not isinstance(pos, dict):
                    continue
                if "yandex" in pos_key.lower() or pos_key.endswith("_1"):
                    try:
                        position = pos.get("position")
                        if position:
                            result[text] = float(position)
                    except (TypeError, ValueError):
                        pass
                    break
    except Exception as e:
        log.warning("Topvisor: ответ не парсится — %s", e)

    log.info("Topvisor: получено %d позиций (если 0 — Topvisor ещё не делал проверку позиций)", len(result))
    return result


# ───── Сохранение / дельта ──────────────────────────────────────────

def save_snapshot(date: str, rows: dict[str, RankRow]) -> Path:
    RANKINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = RANKINGS_DIR / f"{date}.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "keyword", "google_pos", "google_clicks", "google_impressions", "yandex_pos"
        ])
        writer.writeheader()
        for row in rows.values():
            writer.writerow(row.to_dict())
    log.info("Снимок: %s", path)
    return path


def load_previous_snapshot(before_date: str) -> dict[str, RankRow]:
    """Найти предыдущий снимок (любая дата < before_date в data/rankings/)."""
    if not RANKINGS_DIR.exists():
        return {}
    candidates = sorted(
        [p for p in RANKINGS_DIR.glob("*.csv") if p.stem < before_date],
        reverse=True,
    )
    if not candidates:
        return {}
    log.info("Сравнение с предыдущим снимком: %s", candidates[0].name)

    result: dict[str, RankRow] = {}
    with candidates[0].open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            kw = row["keyword"]
            def _f(v):
                try:
                    return float(v) if v else None
                except ValueError:
                    return None
            result[kw] = RankRow(
                keyword=kw,
                google_pos=_f(row.get("google_pos")),
                google_clicks=int(row.get("google_clicks") or 0),
                google_impressions=int(row.get("google_impressions") or 0),
                yandex_pos=_f(row.get("yandex_pos")),
            )
    return result


def compute_changes(current: dict[str, RankRow], previous: dict[str, RankRow]) -> dict:
    """Найти крупные движения позиций между прогонами."""
    risers: list[tuple[str, float, float, str]] = []  # kw, old, new, engine
    fallers: list[tuple[str, float, float, str]] = []
    lost_top10: list[tuple[str, float, float, str]] = []
    new_top10: list[tuple[str, float, float, str]] = []

    for kw, curr in current.items():
        prev = previous.get(kw)
        if not prev:
            continue

        for engine in ("google", "yandex"):
            old = getattr(prev, f"{engine}_pos")
            new = getattr(curr, f"{engine}_pos")
            if old is None or new is None:
                continue
            diff = new - old  # положительное = упал ниже (хуже)
            if diff >= RANK_DROP_THRESHOLD:
                fallers.append((kw, old, new, engine))
            elif diff <= -RANK_DROP_THRESHOLD:
                risers.append((kw, old, new, engine))
            if old <= 10 and new > 10:
                lost_top10.append((kw, old, new, engine))
            if old > 10 and new <= 10:
                new_top10.append((kw, old, new, engine))

    return {
        "risers": sorted(risers, key=lambda x: x[1] - x[2], reverse=True),
        "fallers": sorted(fallers, key=lambda x: x[2] - x[1], reverse=True),
        "lost_top10": lost_top10,
        "new_top10": new_top10,
    }


def render_telegram_changes(changes: dict, current: dict, date: str) -> str:
    has_changes = (changes["risers"] or changes["fallers"]
                   or changes["lost_top10"] or changes["new_top10"])

    in_top10_g = sum(1 for r in current.values() if r.google_pos and r.google_pos <= 10)
    in_top30_g = sum(1 for r in current.values() if r.google_pos and r.google_pos <= 30)
    total_tracked = len(current)

    from notifiers.humanize import plural

    def eng_ru(eng: str) -> str:
        return "Яндекс" if eng == "yandex" else "Google"

    def move(kw, old, new, eng):
        # «запрос: было 18-е → стало 7-е место (Google)»
        return f"  • «{kw}»: было {old:.0f}-е → стало {new:.0f}-е место ({eng_ru(eng)})"

    lines = [f"📊 Позиции сайта в поиске · {date}"]
    lines.append(f"\nОтслеживаем {total_tracked} "
                 f"{plural(total_tracked, 'поисковый запрос', 'поисковых запроса', 'поисковых запросов')}.")
    lines.append(f"В Google на первой странице (ТОП-10): {in_top10_g} · в первых трёх десятках (ТОП-30): {in_top30_g}")
    in_top10_y = sum(1 for r in current.values() if r.yandex_pos and r.yandex_pos <= 10)
    if in_top10_y:
        in_top30_y = sum(1 for r in current.values() if r.yandex_pos and r.yandex_pos <= 30)
        lines.append(f"В Яндексе на первой странице (ТОП-10): {in_top10_y} · в ТОП-30: {in_top30_y}")

    if not has_changes:
        lines.append("\n✅ Резких движений за период нет — позиции держатся ровно.")
        return "\n".join(lines)

    if changes["new_top10"]:
        n = len(changes["new_top10"])
        lines.append(f"\n🟢 Поднялись на первую страницу поиска ({n}) — это хорошо:")
        for kw, old, new, eng in changes["new_top10"][:5]:
            lines.append(move(kw, old, new, eng))

    if changes["risers"]:
        lines.append(f"\n🔼 Заметно выросли ({len(changes['risers'])}):")
        for kw, old, new, eng in changes["risers"][:5]:
            lines.append(move(kw, old, new, eng))

    if changes["lost_top10"]:
        n = len(changes["lost_top10"])
        lines.append(f"\n🟠 Выпали с первой страницы ({n}) — стоит присмотреться:")
        for kw, old, new, eng in changes["lost_top10"][:5]:
            lines.append(move(kw, old, new, eng))

    if changes["fallers"]:
        lines.append(f"\n🔻 Заметно просели ({len(changes['fallers'])}):")
        for kw, old, new, eng in changes["fallers"][:5]:
            lines.append(move(kw, old, new, eng))

    lines.append("\nЧем меньше номер места, тем выше сайт в поиске. Движение в пределах "
                 "пары позиций — это норма, реагируем на резкие падения.")
    return "\n".join(lines)


# ───── Main ─────────────────────────────────────────────────────────

def run_rankings(refresh_seeds: bool = False, dry_run: bool = False) -> dict:
    today = dt.date.today().isoformat()
    log.info("=== M3 rank tracker · %s ===", today)

    if refresh_seeds or not SEEDS_FILE.exists():
        seeds = collect_seed_keywords()
        save_seeds(seeds)
    else:
        seeds = load_seeds()
        log.info("Seed-keywords: %d (из %s)", len(seeds), SEEDS_FILE.name)

    # GSC (Google)
    rows = fetch_gsc_positions(seeds)

    # Topvisor (Яндекс), если доступен
    yandex_pos = fetch_topvisor_positions(seeds)
    for kw, pos in yandex_pos.items():
        if kw in rows and pos:
            rows[kw].yandex_pos = pos

    save_snapshot(today, rows)
    previous = load_previous_snapshot(before_date=today)
    changes = compute_changes(rows, previous)
    log.info("Изменений: risers %d · fallers %d · lost_top10 %d · new_top10 %d",
             len(changes["risers"]), len(changes["fallers"]),
             len(changes["lost_top10"]), len(changes["new_top10"]))

    if not dry_run and os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        from notifiers.telegram import send_telegram
        send_telegram(render_telegram_changes(changes, rows, today))
        log.info("Алерт отправлен в Telegram")

    return {
        "date": today,
        "tracked": len(rows),
        "google_top10": sum(1 for r in rows.values() if r.google_pos and r.google_pos <= 10),
        "google_top30": sum(1 for r in rows.values() if r.google_pos and r.google_pos <= 30),
        "yandex_top10": sum(1 for r in rows.values() if r.yandex_pos and r.yandex_pos <= 10),
        "changes": {k: len(v) for k, v in changes.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="M3 rank tracker")
    parser.add_argument("--refresh-seeds", action="store_true",
                        help="Пересобрать список ключей из MDX + CSV")
    parser.add_argument("--dry-run", action="store_true", help="Не слать в Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    result = run_rankings(refresh_seeds=args.refresh_seeds, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
