"""
M4 — E-E-A-T watchdog (Experience, Expertise, Authoritativeness, Trustworthiness).

Что делает:
  - Сканирует все статьи блога: frontmatter + содержимое.
  - Проверяет наличие:
      * author.name, author.role
      * lastmod (для алгоритма Helpful Content Update)
      * минимум 2 внутренних ссылок на /blog/* или флагманы
      * минимум 1 ссылки на внешний нормативный документ (consultant.ru,
        edu.gov.ru, минпросвещения, ФГОС) для YMYL-статей
      * длина статьи ≥ 1500 слов
      * FAQ-блок (markdown header «Частые вопросы» или JSON-LD FAQPage)
  - Сохраняет per-article score в data/eeat/YYYY-MM-DD.json.
  - Опционально: открывает GitHub Issue с конкретными правками для статей,
    у которых score < 60.

Запуск:
    python3 -m modules.m4_eeat              # сканировать все статьи
    python3 -m modules.m4_eeat --dry-run    # без issue в GitHub
    python3 -m modules.m4_eeat --since 2026-05-15  # только новые

ENV:
    SEO_AGENT_GITHUB_TOKEN — для авто-открытия Issues (опц.)
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID — для алерта по топу
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(THIS_DIR.parent))

log = logging.getLogger(__name__)

REPO_ROOT = THIS_DIR.parent.parent
BLOG_DIR = REPO_ROOT / "content" / "blog"
DATA_DIR = THIS_DIR.parent / "data" / "eeat"

NORMATIVE_DOMAINS = [
    "consultant.ru", "edu.gov.ru", "минпросвещения", "fgos", "фгос",
    "rosobrnadzor", "рособрнадзор", "garant.ru", "publication.pravo.gov.ru",
]


@dataclass
class ArticleScore:
    slug: str
    score: int
    has_author: bool
    has_role: bool
    has_lastmod: bool
    word_count: int
    internal_links: int
    external_links: int
    normative_links: int
    has_faq: bool
    issues: list[str]

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ───── Парсер frontmatter (без YAML lib для устойчивости) ──────────

def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    fm_text = text[4:end]
    body = text[end + 4:]

    fm: dict = {}
    current_key = None
    in_subblock = False
    for line in fm_text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # author: \n  name: "..."  → author.name
        if re.match(r"^[a-zA-Z_]+:\s*$", line):
            current_key = line.strip().rstrip(":")
            in_subblock = True
            fm[current_key] = {}
            continue
        if in_subblock and line.startswith("  ") and ":" in line:
            k, v = line.split(":", 1)
            fm[current_key][k.strip()] = v.strip().strip('"\'')
            continue
        # k: v
        m = re.match(r'^([a-zA-Z_]+):\s*(.+?)$', line)
        if m:
            in_subblock = False
            current_key = m.group(1)
            fm[m.group(1)] = m.group(2).strip().strip('"\'')

    return fm, body


# ───── Проверки ─────────────────────────────────────────────────────

INTERNAL_LINK_RE = re.compile(r'\]\((/[a-zа-я0-9\-/_#]+)\)', re.IGNORECASE)
EXTERNAL_LINK_RE = re.compile(r'\]\((https?://[^\)]+)\)')
FAQ_HEADER_RE = re.compile(r'^##+\s*(частые вопросы|faq|вопросы и ответы)', re.IGNORECASE | re.MULTILINE)


def score_article(path: Path) -> ArticleScore:
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    slug = fm.get("slug", path.stem)

    issues: list[str] = []

    # Author
    author = fm.get("author")
    has_author = isinstance(author, dict) and bool(author.get("name", "").strip())
    has_role = isinstance(author, dict) and bool(author.get("role", "").strip())
    if not has_author:
        issues.append("отсутствует author.name")
    if not has_role:
        issues.append("отсутствует author.role")

    # Lastmod
    has_lastmod = bool(fm.get("lastmod"))
    if not has_lastmod:
        issues.append("отсутствует lastmod (важно для Helpful Content Update)")

    # Внутренние и внешние ссылки
    internal = INTERNAL_LINK_RE.findall(body)
    external = EXTERNAL_LINK_RE.findall(body)
    internal_count = len(internal)
    external_count = len(external)
    normative_count = sum(
        1 for url in external
        if any(dom in url.lower() for dom in NORMATIVE_DOMAINS)
    )

    if internal_count < 2:
        issues.append(f"внутренних ссылок {internal_count} (мин. 2)")
    if normative_count < 1:
        issues.append("нет ссылок на нормативные документы (consultant, edu.gov, минпросвещения)")

    # Объём
    word_count = len(re.findall(r"\b[\w-]+\b", body))
    if word_count < 1500:
        issues.append(f"объём {word_count} слов (мин. 1500)")

    # FAQ
    has_faq = bool(FAQ_HEADER_RE.search(body))
    if not has_faq:
        issues.append("нет FAQ-блока («Частые вопросы»)")

    # Оценка 0-100
    score = 0
    if has_author: score += 15
    if has_role: score += 10
    if has_lastmod: score += 10
    if internal_count >= 2: score += 15
    elif internal_count >= 1: score += 8
    if normative_count >= 1: score += 15
    if word_count >= 1500: score += 20
    elif word_count >= 800: score += 10
    if has_faq: score += 15

    return ArticleScore(
        slug=slug, score=score,
        has_author=has_author, has_role=has_role, has_lastmod=has_lastmod,
        word_count=word_count, internal_links=internal_count,
        external_links=external_count, normative_links=normative_count,
        has_faq=has_faq, issues=issues,
    )


# ───── Авто-issue в GitHub ──────────────────────────────────────────

def open_github_issue(score: ArticleScore, repo: str = os.environ.get("GITHUB_REPOSITORY", "your-org/your-repo")) -> Optional[str]:
    token = os.environ.get("SEO_AGENT_GITHUB_TOKEN", "").strip()
    if not token:
        log.debug("SEO_AGENT_GITHUB_TOKEN не задан — пропускаю issue для %s", score.slug)
        return None

    import requests
    body = (
        f"## E-E-A-T score для статьи {score.slug}\n\n"
        f"**Текущий score:** {score.score}/100\n\n"
        f"### Что нужно поправить\n"
    )
    for issue in score.issues:
        body += f"- [ ] {issue}\n"
    body += (
        f"\n### Метрики\n"
        f"- Слов: {score.word_count}\n"
        f"- Внутренних ссылок: {score.internal_links}\n"
        f"- Внешних ссылок: {score.external_links} (из них нормативных: {score.normative_links})\n"
        f"- FAQ-блок: {'есть' if score.has_faq else 'нет'}\n"
        f"\nФайл: `content/blog/{score.slug}.mdx`\n\n"
        f"_Автоматически открыто seo-agent · M4 E-E-A-T watchdog._"
    )
    response = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": f"[E-E-A-T] {score.slug} — score {score.score}/100",
              "body": body, "labels": ["seo-agent", "eeat"]},
        timeout=15,
    )
    if response.status_code == 201:
        return response.json().get("html_url")
    log.warning("GitHub issue API: %s %s", response.status_code, response.text[:200])
    return None


# ───── Main ─────────────────────────────────────────────────────────

def run_eeat(since: Optional[str] = None, dry_run: bool = False, top_n_alert: int = 5) -> Path:
    today = dt.date.today().isoformat()
    log.info("=== M4 E-E-A-T watchdog · %s ===", today)

    articles = sorted(BLOG_DIR.glob("*.mdx"))
    if since:
        articles = [p for p in articles if _date_from_frontmatter(p) >= since]
    log.info("Сканирую %d статей", len(articles))

    scores: list[ArticleScore] = [score_article(p) for p in articles]
    scores.sort(key=lambda s: s.score)

    # Summary
    avg = sum(s.score for s in scores) / max(len(scores), 1)
    below_60 = [s for s in scores if s.score < 60]
    above_80 = [s for s in scores if s.score >= 80]
    log.info("Средний score: %.1f · ниже 60: %d · выше 80: %d", avg, len(below_60), len(above_80))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = DATA_DIR / f"{today}.json"
    snapshot_path.write_text(
        json.dumps({
            "date": today,
            "total_articles": len(scores),
            "average_score": round(avg, 1),
            "scores": [s.to_dict() for s in scores],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("Снимок: %s", snapshot_path)

    # Issues для top-N худших
    issue_urls: list[str] = []
    if not dry_run and os.environ.get("SEO_AGENT_GITHUB_TOKEN"):
        for s in scores[:top_n_alert]:
            if s.score >= 60:
                continue
            url = open_github_issue(s)
            if url:
                issue_urls.append(url)
                log.info("Открыт issue: %s", url)

    # Telegram
    if not dry_run and os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        from notifiers.telegram import send_telegram
        lines = [f"🛡 Насколько статьи выглядят экспертно · {today}"]
        lines.append("\nПроверяем, доверяет ли поисковик нашим статьям: есть ли автор, "
                     "источники, факты, дата. Оценка каждой статьи — от 0 до 100, "
                     "чем выше, тем лучше.")
        lines.append(f"\nВсего статей: {len(scores)} · средняя оценка: {avg:.0f} из 100")
        lines.append(f"Слабых (ниже 60): {len(below_60)} · сильных (80 и выше): {len(above_80)}")
        if scores[:5] and scores[0].score < 60:
            lines.append("\nСтатьи, которые стоит усилить (автор, источники, факты):")
            for s in scores[:5]:
                lines.append(f"  • {s.score} из 100 — {s.slug}")
        if issue_urls:
            lines.append(f"\nЗавёл {len(issue_urls)} задач на доработку (в GitHub).")
        send_telegram("\n".join(lines))

    return snapshot_path


def _date_from_frontmatter(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(r'^date:\s*["\']?(\d{4}-\d{2}-\d{2})', text, flags=re.MULTILINE)
    return m.group(1) if m else "1970-01-01"


def main() -> int:
    parser = argparse.ArgumentParser(description="M4 E-E-A-T watchdog")
    parser.add_argument("--since", type=str, default=None, help="ISO дата (например 2026-05-15)")
    parser.add_argument("--dry-run", action="store_true", help="Не открывать issues, не слать Telegram")
    parser.add_argument("--top-n-issues", type=int, default=5,
                        help="Сколько worst-articles обработать (issues)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_eeat(since=args.since, dry_run=args.dry_run, top_n_alert=args.top_n_issues)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
