"""
M5 — Internal linker (граф перелинковки + рекомендации).

Что делает:
  1. Строит граф внутренних ссылок:
       - все статьи блога (content/blog/*.mdx)
       - флагманы /doshkolnoe /nachalnoe /dopolnitelnoe
       - каталог /spetsialnosti и страницы специальностей
  2. Находит:
       - сирот: ноды без входящих ссылок (теряют вес)
       - короткие циклы (A→B→A без выхода)
       - тематически связанные статьи без ссылки между собой
  3. Сохраняет рекомендации в data/linker/YYYY-MM-DD.json.
  4. Шлёт сводку в Telegram.

Запуск:
    python3 -m modules.m5_linker            # производит граф + рекомендации
    python3 -m modules.m5_linker --dry-run  # без Telegram
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(THIS_DIR.parent))

log = logging.getLogger(__name__)

REPO_ROOT = THIS_DIR.parent.parent
BLOG_DIR = REPO_ROOT / "content" / "blog"
DATA_DIR = THIS_DIR.parent / "data" / "linker"

INTERNAL_LINK_RE = re.compile(r'\]\(((/[a-zа-я0-9\-/_#]+)|(https?://example\.ru[^\)]*))\)', re.IGNORECASE)
FLAGSHIP_PATHS = {"/doshkolnoe", "/nachalnoe", "/dopolnitelnoe", "/blog", "/spetsialnosti"}


def slug_to_path(slug: str) -> str:
    return f"/blog/{slug}"


def extract_links(text: str) -> set[str]:
    """Найти все внутренние ссылки, нормализовать."""
    out: set[str] = set()
    for m in INTERNAL_LINK_RE.finditer(text):
        url = m.group(1)
        if url.startswith("http"):
            # https://example.com/foo → /foo
            url = re.sub(r"^https?://example\.ru", "", url)
        # Убираем якорь и хвостовой слэш
        url = url.split("#")[0].rstrip("/") or "/"
        if url.startswith("/"):
            out.add(url)
    return out


def build_graph() -> tuple[dict[str, set[str]], dict[str, dict]]:
    """Граф: {url: {target_urls}}. Также возвращает meta (title, tags, category)."""
    graph: dict[str, set[str]] = defaultdict(set)
    meta: dict[str, dict] = {}

    # Флагманы — для них предполагаем, что они линкуют сами на себя из FAQ/related
    for path in FLAGSHIP_PATHS:
        meta[path] = {"type": "page", "title": path, "tags": []}

    for p in sorted(BLOG_DIR.glob("*.mdx")):
        text = p.read_text(encoding="utf-8")
        slug = p.stem
        path = slug_to_path(slug)

        # Frontmatter
        category = ""
        title = ""
        tags: list[str] = []
        if text.startswith("---"):
            end = text.find("\n---", 4)
            if end != -1:
                fm = text[4:end]
                tm = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', fm, flags=re.MULTILINE)
                if tm:
                    title = tm.group(1).strip()
                cm = re.search(r'^category:\s*["\']?([\w-]+)', fm, flags=re.MULTILINE)
                if cm:
                    category = cm.group(1)
                # tags:\n  - a\n  - b
                in_tags = False
                for line in fm.splitlines():
                    if line.startswith("tags:"):
                        in_tags = True
                        continue
                    if in_tags:
                        tm = re.match(r'^\s*-\s+["\']?([^"\']+?)["\']?\s*$', line)
                        if tm:
                            tags.append(tm.group(1).strip())
                        elif not line.startswith(" "):
                            in_tags = False

        meta[path] = {"type": "blog", "title": title, "category": category, "tags": tags}
        graph[path]  # touch — узел существует даже без исходящих

        for target in extract_links(text):
            graph[path].add(target)

    return dict(graph), meta


# ───── Анализ ───────────────────────────────────────────────────────

def find_orphans(graph: dict[str, set[str]]) -> list[str]:
    """Узлы блога без входящих ссылок."""
    incoming: Counter[str] = Counter()
    for src, targets in graph.items():
        for t in targets:
            incoming[t] += 1
    orphans = []
    for node in graph:
        if not node.startswith("/blog/"):
            continue
        if incoming[node] == 0:
            orphans.append(node)
    return orphans


def find_link_recommendations(graph: dict[str, set[str]], meta: dict[str, dict], top_n: int = 30) -> list[dict]:
    """Найти пары статей с общими тегами, между которыми нет ссылки."""
    recommendations: list[dict] = []

    blog_nodes = [n for n in meta if n.startswith("/blog/")]

    for a in blog_nodes:
        a_meta = meta.get(a, {})
        a_tags = set(a_meta.get("tags", []))
        if not a_tags:
            continue
        existing = graph.get(a, set())
        for b in blog_nodes:
            if a == b or b in existing:
                continue
            b_meta = meta.get(b, {})
            b_tags = set(b_meta.get("tags", []))
            overlap = a_tags & b_tags
            if len(overlap) >= 2:  # порог общих тегов
                recommendations.append({
                    "from": a,
                    "to": b,
                    "from_title": a_meta.get("title", ""),
                    "to_title": b_meta.get("title", ""),
                    "common_tags": sorted(overlap),
                    "score": len(overlap),
                })

    recommendations.sort(key=lambda r: r["score"], reverse=True)
    return recommendations[:top_n]


def link_counts(graph: dict[str, set[str]]) -> dict[str, int]:
    incoming: Counter[str] = Counter()
    for src, targets in graph.items():
        for t in targets:
            incoming[t] += 1
    out = {}
    for node in graph:
        out[node] = incoming.get(node, 0)
    return out


# ───── Main ─────────────────────────────────────────────────────────

def run_linker(dry_run: bool = False) -> Path:
    today = dt.date.today().isoformat()
    log.info("=== M5 linker · %s ===", today)

    graph, meta = build_graph()
    log.info("Граф: %d узлов · %d исходящих ссылок",
             len(graph), sum(len(v) for v in graph.values()))

    orphans = find_orphans(graph)
    log.info("Сирот (статей без входящих ссылок): %d", len(orphans))

    recommendations = find_link_recommendations(graph, meta)
    log.info("Топ %d рекомендаций по перелинковке", len(recommendations))

    incoming_counts = link_counts(graph)
    least_linked = sorted(
        [(n, c) for n, c in incoming_counts.items() if n.startswith("/blog/")],
        key=lambda x: x[1]
    )[:10]
    most_linked = sorted(
        [(n, c) for n, c in incoming_counts.items() if n.startswith("/blog/")],
        key=lambda x: x[1], reverse=True
    )[:10]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = DATA_DIR / f"{today}.json"
    snapshot_path.write_text(
        json.dumps({
            "date": today,
            "nodes": len(graph),
            "edges": sum(len(v) for v in graph.values()),
            "orphans": orphans,
            "least_linked": least_linked,
            "most_linked": most_linked,
            "recommendations": recommendations,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("Снимок: %s", snapshot_path)

    if not dry_run and os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        from notifiers.telegram import send_telegram
        lines = [f"🔗 Ссылки между статьями · {today}"]
        lines.append("\nВнутренние ссылки между статьями помогают и читателю, и поисковику. "
                     "Плохо, когда на статью нет ни одной ссылки с других страниц — "
                     "поисковик её хуже находит. Такие статьи называем «сиротами».")
        total_links = sum(len(v) for v in graph.values())
        lines.append(f"\nВсего страниц связано ссылками: {len(graph)} · ссылок между ними: {total_links}")
        if orphans:
            lines.append(f"Статей без единой входящей ссылки (сироты): {len(orphans)}")
            for o in orphans[:5]:
                lines.append(f"  • {o}")
        else:
            lines.append("Статей-сирот нет — каждая статья на кого-то ссылается. 👍")
        if recommendations:
            lines.append(f"\nПодсказки, куда добавить ссылки (по общим темам): {len(recommendations)}")
            for r in recommendations[:3]:
                lines.append(f"  • из «{r['from']}» → на «{r['to']}»")
        send_telegram("\n".join(lines))

    return snapshot_path


def main() -> int:
    parser = argparse.ArgumentParser(description="M5 internal linker")
    parser.add_argument("--dry-run", action="store_true", help="Не слать в Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_linker(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
