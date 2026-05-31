"""
M1 — Semantic collector.

Что делает:
  1. Собирает поисковые запросы из GSC за последние 30 дней.
  2. Собирает поисковые запросы из Я.Вебмастер за последние 30 дней.
  3. Дедуплицирует и фильтрует шум (≥3 показов).
  4. Сравнивает с уже использованными темами (content-factory/data/topics_used.csv)
     и текущим бэклогом (topics_backlog.csv).
  5. Группирует новые запросы по кластерам через Claude API.
  6. Записывает новые темы в content-factory/data/topics_backlog.csv.

Запуск:
    python3 -m modules.m1_semantic            # production
    python3 -m modules.m1_semantic --dry-run  # без записи в CSV и Telegram
    python3 -m modules.m1_semantic --days 60  # глубина истории

ENV:
    GSC_OAUTH_REFRESH_TOKEN, GSC_OAUTH_CLIENT_ID, GSC_OAUTH_CLIENT_SECRET
    YANDEX_WEBMASTER_TOKEN
    ANTHROPIC_API_KEY
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (опц.)
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
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(THIS_DIR.parent))

from modules.gsc_client import gsc_service, query_search_analytics  # noqa: E402
from modules.yandex_webmaster import yw_get_user_id, yw_list_hosts, yw_search_queries  # noqa: E402

log = logging.getLogger(__name__)

REPO_ROOT = THIS_DIR.parent.parent
CONTENT_FACTORY = REPO_ROOT / "content-factory" / "data"
BACKLOG_CSV = CONTENT_FACTORY / "topics_backlog.csv"
USED_CSV = CONTENT_FACTORY / "topics_used.csv"
SEMANTIC_DIR = THIS_DIR.parent / "data" / "semantic"

# Категории, в которые M1 может класть темы — должны совпадать с content-factory.
KNOWN_CATEGORIES = {"admission", "career", "dictionary", "extra", "preschool", "primary", "profession"}

# Сколько query забирать.
GSC_QUERY_LIMIT = 1000
YW_QUERY_LIMIT = 500  # Я.Вебмастер сам ограничивает топ-N

# Фильтрация шума.
MIN_IMPRESSIONS = 3

# Сколько новых тем добавлять в бэклог за один прогон (чтобы не утопить content-factory).
MAX_NEW_TOPICS_PER_RUN = 30


# ───── Сбор query ───────────────────────────────────────────────────

@dataclass
class QueryStat:
    text: str
    impressions: int = 0
    clicks: int = 0
    position: float = 0.0
    source: str = ""  # "gsc", "yw", "both"

    def merge(self, other: "QueryStat") -> None:
        self.impressions += other.impressions
        self.clicks += other.clicks
        if other.position:
            self.position = (self.position + other.position) / 2 if self.position else other.position
        self.source = "both" if self.source and self.source != other.source else (self.source or other.source)


def collect_gsc_queries(days: int) -> list[QueryStat]:
    end_date = (dt.date.today() - dt.timedelta(days=2)).isoformat()  # GSC lag ~2 дня
    start_date = (dt.date.today() - dt.timedelta(days=days + 2)).isoformat()
    site_url = os.environ.get("GSC_SITE_URL", "sc-domain:example.com")
    log.info("GSC: запрашиваю query %s → %s", start_date, end_date)

    try:
        svc = gsc_service()
        rows = query_search_analytics(svc, site_url, start_date, end_date, ["query"], row_limit=GSC_QUERY_LIMIT)
    except Exception as e:
        log.warning("GSC не отдал query: %s", e)
        return []

    result: list[QueryStat] = []
    for row in rows:
        query_text = row["keys"][0].strip().lower()
        if not query_text or row.get("impressions", 0) < MIN_IMPRESSIONS:
            continue
        result.append(QueryStat(
            text=query_text,
            impressions=int(row.get("impressions", 0)),
            clicks=int(row.get("clicks", 0)),
            position=float(row.get("position", 0)),
            source="gsc",
        ))
    log.info("GSC: получено %d query (≥%d показов)", len(result), MIN_IMPRESSIONS)
    return result


def collect_yw_queries(days: int) -> list[QueryStat]:
    # Я.Вебмастер data lag ~3 дня
    date_to = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    date_from = (dt.date.today() - dt.timedelta(days=days + 3)).isoformat()

    try:
        user_id = yw_get_user_id()
        hosts = yw_list_hosts(user_id)
        host_id = None
        for h in hosts:
            url = h.get("unicode_host_url", "")
            if "example.com" in url and url.startswith("https"):
                host_id = h["host_id"]
                break
        if not host_id:
            log.warning("Я.Вебмастер: example.com не найден среди подтверждённых")
            return []
        log.info("Я.Вебмастер: query %s → %s, host_id=%s", date_from, date_to, host_id)
        queries = yw_search_queries(user_id, host_id, date_from, date_to, limit=YW_QUERY_LIMIT)
    except Exception as e:
        log.warning("Я.Вебмастер не отдал query: %s", e)
        return []

    result: list[QueryStat] = []
    for q in queries:
        text = (q.get("query_text") or "").strip().lower()
        if not text:
            continue
        ind = q.get("indicators", {})
        shows = int(ind.get("TOTAL_SHOWS", 0))
        clicks = int(ind.get("TOTAL_CLICKS", 0))
        if shows < MIN_IMPRESSIONS:
            continue
        result.append(QueryStat(
            text=text,
            impressions=shows,
            clicks=clicks,
            position=float(ind.get("AVG_SHOW_POSITION", 0) or 0),
            source="yw",
        ))
    log.info("Я.Вебмастер: получено %d query", len(result))
    return result


def merge_queries(*sources: list[QueryStat]) -> dict[str, QueryStat]:
    """Слить query из нескольких источников по нормализованному тексту."""
    merged: dict[str, QueryStat] = {}
    for src in sources:
        for q in src:
            norm = re.sub(r"\s+", " ", q.text).strip().lower()
            if norm in merged:
                merged[norm].merge(q)
            else:
                merged[norm] = QueryStat(
                    text=norm,
                    impressions=q.impressions,
                    clicks=q.clicks,
                    position=q.position,
                    source=q.source,
                )
    return merged


# ───── Фильтрация: уже используется ─────────────────────────────────

def load_used_keywords() -> set[str]:
    """Все primary_keyword из используемых тем — для отсеивания дубликатов."""
    used: set[str] = set()
    for path in [USED_CSV, BACKLOG_CSV]:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                kw = (row.get("primary_keyword") or "").strip().lower()
                if kw:
                    used.add(kw)
    return used


def looks_like_used(query: str, used: set[str]) -> bool:
    """Близкое совпадение query с уже использованным ключом."""
    q = query.lower()
    for u in used:
        if not u:
            continue
        # Простая проверка: query содержит >=70% слов из used keyword
        u_words = set(u.split())
        if not u_words:
            continue
        q_words = set(q.split())
        overlap = len(u_words & q_words)
        if overlap / len(u_words) >= 0.7:
            return True
    return False


def filter_brand_queries(queries: dict[str, QueryStat]) -> dict[str, QueryStat]:
    """Брендовые query — отсеиваем (не нужно генерить контент про себя)."""
    BRAND_TERMS = ["<<your-brand>>"]  # подставь брендовые слова своего сайта
    out = {}
    for k, v in queries.items():
        if any(term in k for term in BRAND_TERMS):
            continue
        out[k] = v
    return out


# ───── Кластеризация через Claude ───────────────────────────────────

CLUSTER_PROMPT = """Ты — SEO-стратег сайта. На вход — список поисковых запросов из
Яндекс.Вебмастера и Google Search Console, по которым сайт показывался, но ещё не имеет
соответствующей статьи.

Сгруппируй их в кластеры по интенту и теме. Для каждого кластера предложи **одну** тему
статьи блога. Цель — статьи, которые соберут трафик и приведут заявки.

Категории (ПРИМЕР — адаптируй под свою нишу в этом промпте):
- guide — практические руководства, «как сделать X»
- dictionary — что такое X, отличия X от Y
- commercial — выбор, сравнение, цены
- news — новости и обзоры ниши

Intent: informational | commercial | transactional.
Приоритет (1-10): зависит от показов кластера. >100 показов = 8-9, 30-100 = 6-7, <30 = 4-5.

Верни JSON-массив объектов:
[
  {
    "topic": "Полный заголовок будущей статьи (1 предложение, до 80 символов)",
    "primary_keyword": "главный ключ кластера в lowercase",
    "secondary_keywords": "до 3 LSI-ключей через запятую",
    "intent": "informational",
    "category": "guide",
    "wordstat_frequency": 0,
    "competitor_refs": "",
    "priority": 7,
    "_source_queries": ["query 1", "query 2"],
    "_total_impressions": 123
  }
]

ВАЖНО:
- Даже 1 запрос с >50 показов может быть отдельной темой.
- Не предлагай брендовые темы про сам сайт — только про нишу в целом.

ЗАПРОСЫ (формат: query · показов / кликов / средняя позиция):
"""


def cluster_with_claude(queries: dict[str, QueryStat], max_topics: int = MAX_NEW_TOPICS_PER_RUN) -> list[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.error("ANTHROPIC_API_KEY не задан — кластеризация невозможна")
        return []

    # Отбираем top-N query по показам — больше отдавать в LLM нет смысла
    sorted_qs = sorted(queries.values(), key=lambda x: x.impressions, reverse=True)[:200]
    if not sorted_qs:
        return []

    rows = [f"- «{q.text}» · {q.impressions} показов / {q.clicks} кликов / поз. {q.position:.1f} ({q.source})"
            for q in sorted_qs]
    user_prompt = CLUSTER_PROMPT + "\n".join(rows) + (
        f"\n\nСгруппируй в кластеры и верни до {max_topics} тем. Только валидный JSON, без markdown-обёртки."
    )

    try:
        import anthropic
    except ImportError:
        log.error("anthropic SDK не установлен — pip install anthropic")
        return []

    client = anthropic.Anthropic(api_key=api_key)
    log.info("Claude кластеризация %d query (отобрано из %d)...", len(sorted_qs), len(queries))
    try:
        response = client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            max_tokens=8000,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        log.error("Claude API error: %s", e)
        return []

    text = response.content[0].text.strip() if response.content else ""
    # Иногда модель оборачивает в ```json — снимаем
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

    try:
        topics = json.loads(text)
    except json.JSONDecodeError as e:
        log.error("Claude ответ не парсится как JSON: %s\n%s", e, text[:500])
        return []

    if not isinstance(topics, list):
        log.error("Claude вернул не массив: %s", type(topics))
        return []

    # Валидация полей
    valid_topics: list[dict] = []
    for t in topics:
        if not isinstance(t, dict):
            continue
        if not all(k in t for k in ("topic", "primary_keyword", "category", "intent", "priority")):
            continue
        if t["category"] not in KNOWN_CATEGORIES:
            log.warning("Неизвестная категория %r у темы %r — пропускаю", t.get("category"), t.get("topic"))
            continue
        valid_topics.append(t)

    log.info("Claude вернул %d валидных кластеров (из %d сырых)", len(valid_topics), len(topics))
    return valid_topics[:max_topics]


# ───── Запись в backlog ─────────────────────────────────────────────

def append_to_backlog(topics: list[dict], dry_run: bool = False) -> int:
    if not topics:
        return 0
    if dry_run:
        log.info("dry-run: пропущено %d тем для добавления:", len(topics))
        for t in topics:
            log.info("  • [%s] %s (приоритет %s)", t["category"], t["topic"], t.get("priority"))
        return 0

    fieldnames = ["topic", "primary_keyword", "secondary_keywords", "intent",
                  "category", "wordstat_frequency", "competitor_refs", "priority"]
    existing_topics = set()
    if BACKLOG_CSV.exists():
        with BACKLOG_CSV.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing_topics.add((row.get("primary_keyword") or "").strip().lower())

    added = 0
    with BACKLOG_CSV.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        for t in topics:
            kw = (t["primary_keyword"] or "").strip().lower()
            if kw in existing_topics:
                continue
            row = {k: t.get(k, "") for k in fieldnames}
            writer.writerow(row)
            added += 1
    log.info("Добавлено в %s: %d новых тем", BACKLOG_CSV.relative_to(REPO_ROOT), added)
    return added


def save_semantic_snapshot(queries: dict[str, QueryStat], topics: list[dict]) -> Path:
    SEMANTIC_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    payload = {
        "date": today,
        "queries_count": len(queries),
        "topics_added": len(topics),
        "queries": [
            {"text": q.text, "impressions": q.impressions, "clicks": q.clicks,
             "position": q.position, "source": q.source}
            for q in sorted(queries.values(), key=lambda x: x.impressions, reverse=True)
        ],
        "topics": topics,
    }
    path = SEMANTIC_DIR / f"{today}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Снимок: %s", path)
    return path


# ───── Main ─────────────────────────────────────────────────────────

def run_collection(days: int = 30, dry_run: bool = False) -> dict:
    log.info("=== M1 semantic collector · %s · окно %d дней ===", dt.date.today(), days)

    gsc = collect_gsc_queries(days)
    yw = collect_yw_queries(days)
    merged = merge_queries(gsc, yw)
    log.info("Объединение: %d уникальных query", len(merged))

    merged = filter_brand_queries(merged)
    log.info("После фильтра брендовых: %d", len(merged))

    used = load_used_keywords()
    log.info("Уже используется/в бэклоге: %d ключей", len(used))

    fresh = {k: v for k, v in merged.items() if not looks_like_used(k, used)}
    log.info("Новых query (нет в used/backlog): %d", len(fresh))

    topics = cluster_with_claude(fresh) if fresh else []
    added = append_to_backlog(topics, dry_run=dry_run)

    snapshot = save_semantic_snapshot(merged, topics)

    # Telegram
    if not dry_run and os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        from notifiers.telegram import send_telegram
        send_telegram(render_telegram(merged, fresh, topics, added))
        log.info("Уведомление отправлено в Telegram")

    return {
        "queries_total": len(merged),
        "queries_fresh": len(fresh),
        "topics_proposed": len(topics),
        "topics_added": added,
        "snapshot": str(snapshot),
    }


def render_telegram(merged: dict, fresh: dict, topics: list[dict], added: int) -> str:
    lines = ["📚 Новые темы для статей · " + dt.date.today().isoformat()]
    lines.append("\nСобрали реальные поисковые запросы людей (из Яндекса и Google) "
                 "и придумали под них темы статей.")
    lines.append(f"Всего запросов разобрали: {len(merged)} · из них новых: {len(fresh)}")
    lines.append(f"Придумали тем: {len(topics)} · поставили в очередь на написание: {added}")
    if topics[:5]:
        lines.append("\nЧто за темы (примеры):")
        for t in topics[:5]:
            lines.append(f"  • {t.get('topic')}")
    if added:
        lines.append("\nСтатьи по этим темам напишутся и опубликуются автоматически "
                     "в ближайшие дни.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="M1 semantic collector")
    parser.add_argument("--days", type=int, default=30, help="Глубина истории (по умолчанию 30)")
    parser.add_argument("--dry-run", action="store_true", help="Не писать в backlog, не слать Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    result = run_collection(days=args.days, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
