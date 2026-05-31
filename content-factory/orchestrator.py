#!/usr/bin/env python3
"""
content-factory orchestrator.

Один прогон в день (по cron), генерирует ARTICLES_PER_DAY статей,
коммитит в college-site-ast main, шлёт email-репорт.

Запуск: python orchestrator.py
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv

from send_email import send_report

# ── Настройка ─────────────────────────────────────────────────────────
load_dotenv()

ROOT = Path(__file__).parent.resolve()
PROMPTS = ROOT / "prompts"
DATA = ROOT / "data"
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)
DATA.mkdir(exist_ok=True)

BACKLOG_PATH = DATA / "topics_backlog.csv"
USED_PATH = DATA / "topics_used.csv"

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
ARTICLES_PER_DAY = int(os.environ.get("ARTICLES_PER_DAY", "5"))
ENABLE_EDITOR_PASS = os.environ.get("ENABLE_EDITOR_PASS", "true").lower() == "true"

# Batch API: −50% на токены (запуск раз в день не критичен по латентности).
# Статьи независимы, поэтому гоняем по стадиям: 5 брифов → 5 текстов → 5 редактур.
# Внутри стадии общий system-промпт кэшируется (prompt caching). USE_BATCH=false
# мгновенно возвращает старый последовательный режим. См. docs/seo (changelog).
USE_BATCH = os.environ.get("USE_BATCH", "true").lower() == "true"
BATCH_POLL_INTERVAL = int(os.environ.get("BATCH_POLL_INTERVAL", "20"))   # сек между опросами
BATCH_MAX_WAIT = int(os.environ.get("BATCH_MAX_WAIT", "5400"))           # макс. ожидание стадии, сек

IS_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"

if IS_GITHUB_ACTIONS:
    # В Actions репо уже checkout-нут в GITHUB_WORKSPACE.
    # Никакого clone делать не нужно — пишем прямо в текущую копию.
    SITE_REPO_PATH = Path(os.environ.get("GITHUB_WORKSPACE", "/github/workspace")).resolve()
    SITE_REPO_URL = ""  # не используется в Actions-режиме
else:
    SITE_REPO_PATH = Path(os.environ["SITE_REPO_PATH"])
    SITE_REPO_URL = os.environ["SITE_REPO_URL"]

SITE_REPO_BRANCH = os.environ.get("SITE_REPO_BRANCH", "main")
SITE_BLOG_DIR = os.environ.get("SITE_BLOG_DIR", "content/blog")

# логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOGS / f"run-{datetime.now():%Y-%m-%d}.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("orchestrator")

client = Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Модели данных ─────────────────────────────────────────────────────
@dataclass
class Topic:
    topic: str
    primary_keyword: str
    secondary_keywords: str
    intent: str
    category: str
    wordstat_frequency: Optional[int]
    competitor_refs: str
    priority: int

    @classmethod
    def from_row(cls, row: dict) -> "Topic":
        freq = row.get("wordstat_frequency", "").strip()
        return cls(
            topic=row["topic"].strip(),
            primary_keyword=row["primary_keyword"].strip(),
            secondary_keywords=row.get("secondary_keywords", "").strip(),
            intent=row.get("intent", "informational").strip() or "informational",
            category=row.get("category", "dictionary").strip() or "dictionary",
            wordstat_frequency=int(freq) if freq.isdigit() else None,
            competitor_refs=row.get("competitor_refs", "").strip(),
            priority=int(row.get("priority", "0").strip() or "0"),
        )


@dataclass
class ArticleResult:
    topic: Topic
    slug: str = ""
    title: str = ""
    file_path: Optional[Path] = None
    status: str = "pending"  # pending | published | draft | failed
    error: Optional[str] = None
    elapsed_sec: float = 0.0


@dataclass
class RunReport:
    started_at: datetime
    finished_at: Optional[datetime] = None
    results: list[ArticleResult] = field(default_factory=list)
    commit_sha: Optional[str] = None


# ── Работа с CSV ──────────────────────────────────────────────────────
def load_backlog() -> list[Topic]:
    if not BACKLOG_PATH.exists():
        log.error("Не найден %s — некому генерировать темы", BACKLOG_PATH)
        return []
    with BACKLOG_PATH.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        topics = [Topic.from_row(r) for r in reader if r.get("topic", "").strip()]
    return topics


def load_used_slugs() -> set[str]:
    if not USED_PATH.exists():
        return set()
    used = set()
    with USED_PATH.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r.get("slug"):
                used.add(r["slug"].strip())
            if r.get("primary_keyword"):
                used.add(r["primary_keyword"].strip().lower())
    return used


def append_used(rows: list[dict]) -> None:
    is_new = not USED_PATH.exists()
    with USED_PATH.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["date", "slug", "title", "primary_keyword", "category", "status", "url"],
        )
        if is_new:
            writer.writeheader()
        for r in rows:
            writer.writerow(r)


def remove_topics_from_backlog(consumed: list[Topic]) -> None:
    consumed_keys = {(t.topic, t.primary_keyword) for t in consumed}
    if not BACKLOG_PATH.exists():
        return
    with BACKLOG_PATH.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        kept = [
            r for r in reader
            if (r.get("topic", "").strip(), r.get("primary_keyword", "").strip()) not in consumed_keys
        ]
    with BACKLOG_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept)


# ── Выбор тем ─────────────────────────────────────────────────────────
def pick_topics(n: int) -> list[Topic]:
    backlog = load_backlog()
    used = load_used_slugs()

    # дедупликация: убираем темы, чей primary_keyword уже встречался
    candidates = [
        t for t in backlog
        if t.primary_keyword.strip().lower() not in used
    ]
    candidates.sort(key=lambda t: (-t.priority, -(t.wordstat_frequency or 0)))
    return candidates[:n]


# ── Промпты и вызов Claude ────────────────────────────────────────────
def read_prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def call_claude(system: str, user: str, max_tokens: int = 8000) -> str:
    """Один вызов Claude Sonnet с retry на сетевые ошибки."""
    last_err = None
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return resp.content[0].text
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("Claude call failed (attempt %d/3): %s", attempt + 1, e)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Claude call failed after 3 attempts: {last_err}")


# Построители user-сообщений и парсер брифа — общие для последовательного
# и батч-режимов, чтобы формат не разъезжался между путями.
def brief_user(topic: Topic, publish_date: str) -> str:
    return (
        f"TOPIC: {topic.topic}\n"
        f"PRIMARY_KEYWORD: {topic.primary_keyword}\n"
        f"SECONDARY_KEYWORDS: {topic.secondary_keywords}\n"
        f"INTENT: {topic.intent}\n"
        f"CATEGORY: {topic.category}\n"
        f"WORDSTAT_FREQUENCY: {topic.wordstat_frequency or 'null'}\n"
        f"COMPETITOR_REFS: {topic.competitor_refs}\n"
        f"PUBLISH_DATE: {publish_date}\n"
    )


def parse_brief(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    return json.loads(raw)


def article_user(brief: dict, publish_date: str) -> str:
    return (
        f"PUBLISH_DATE: {publish_date}\n"
        f"BRIEF (JSON):\n{json.dumps(brief, ensure_ascii=False, indent=2)}\n"
    )


def edit_user(mdx: str) -> str:
    return f"ARTICLE MDX:\n{mdx}"


def make_seo_brief(topic: Topic, publish_date: str) -> dict:
    raw = call_claude(read_prompt("01_seo_brief.md"), brief_user(topic, publish_date), max_tokens=4000)
    return parse_brief(raw)


def make_article_mdx(brief: dict, publish_date: str) -> str:
    raw = call_claude(read_prompt("02_article_writer.md"), article_user(brief, publish_date), max_tokens=8000)
    return raw.strip()


def edit_article_mdx(mdx: str) -> str:
    raw = call_claude(read_prompt("03_editor_factcheck.md"), edit_user(mdx), max_tokens=8000)
    return raw.strip()


# ── Batch API: стадийная генерация (−50% на токенах) ──────────────────
def run_stage_batch(label: str, system_text: str, jobs: list[tuple[str, str]],
                    max_tokens: int) -> dict[str, Optional[str]]:
    """Один батч на стадию. Все запросы делят общий system (кэшируется → reads
    со 2-го запроса). jobs = [(custom_id, user_text)]. Возвращает {custom_id: text|None}.
    """
    if not jobs:
        return {}
    # cache_control на system: внутри батча 5 одинаковых system-префиксов →
    # 1 запись + 4 чтения по ~10% цены. На 03 (короткий) кэш может не сработать
    # (ниже минимума Sonnet) — это ок, просто не даст экономии.
    system_block = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
    requests = [
        {
            "custom_id": cid,
            "params": {
                "model": MODEL,
                "max_tokens": max_tokens,
                "system": system_block,
                "messages": [{"role": "user", "content": user}],
            },
        }
        for cid, user in jobs
    ]
    log.info("Batch[%s]: отправляю %d запросов...", label, len(requests))
    batch = client.messages.batches.create(requests=requests)

    waited = 0
    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        if waited >= BATCH_MAX_WAIT:
            raise RuntimeError(
                f"Batch[{label}] не завершился за {BATCH_MAX_WAIT}s (status={b.processing_status})"
            )
        time.sleep(BATCH_POLL_INTERVAL)
        waited += BATCH_POLL_INTERVAL

    out: dict[str, Optional[str]] = {}
    for r in client.messages.batches.results(batch.id):
        if r.result.type == "succeeded":
            msg = r.result.message
            out[r.custom_id] = next((blk.text for blk in msg.content if blk.type == "text"), "")
        else:
            out[r.custom_id] = None
            log.warning("Batch[%s]: %s → %s", label, r.custom_id, r.result.type)
    ok = sum(1 for v in out.values() if v)
    log.info("Batch[%s]: готово — %d/%d успешно", label, ok, len(requests))
    return out


# ── Git операции ──────────────────────────────────────────────────────
def run(cmd: list[str], cwd: Optional[Path] = None) -> str:
    log.info("$ %s%s", " ".join(cmd), f"  (cwd={cwd})" if cwd else "")
    result = subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)
    if result.stdout:
        log.info(result.stdout.strip())
    return result.stdout.strip()


def ensure_site_repo() -> None:
    """Клонирует или подтягивает репо сайта.

    В режиме GitHub Actions репо уже checkout-нут actions/checkout — ничего делать
    не нужно, пишем прямо в текущий workspace.
    В локальном/VPS режиме клонируем (или fetch+reset) в SITE_REPO_PATH.
    """
    if IS_GITHUB_ACTIONS:
        log.info("GitHub Actions режим: репо уже в %s", SITE_REPO_PATH)
        return
    if not SITE_REPO_PATH.exists():
        SITE_REPO_PATH.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--branch", SITE_REPO_BRANCH, SITE_REPO_URL, str(SITE_REPO_PATH)])
    else:
        run(["git", "fetch", "origin"], cwd=SITE_REPO_PATH)
        run(["git", "checkout", SITE_REPO_BRANCH], cwd=SITE_REPO_PATH)
        run(["git", "reset", "--hard", f"origin/{SITE_REPO_BRANCH}"], cwd=SITE_REPO_PATH)


def commit_and_push(written_files: list[Path], date_str: str) -> Optional[str]:
    if not written_files:
        return None

    # В Actions-режиме также коммитим обновлённые data/topics_*.csv,
    # потому что они живут в самом репо и orchestrator их меняет.
    paths_to_add: list[str] = [str(p.relative_to(SITE_REPO_PATH)) for p in written_files]
    if IS_GITHUB_ACTIONS:
        cf_data = Path("content-factory/data")
        for csv_name in ("topics_backlog.csv", "topics_used.csv"):
            csv_path = SITE_REPO_PATH / cf_data / csv_name
            if csv_path.exists():
                paths_to_add.append(str((cf_data / csv_name)))

    run(["git", "add", "--", *paths_to_add], cwd=SITE_REPO_PATH)
    # проверяем, есть ли что коммитить
    diff = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=SITE_REPO_PATH, capture_output=True, text=True, check=True,
    )
    if not diff.stdout.strip():
        log.warning("Нет изменений для коммита (возможно, статьи дублируют существующие)")
        return None
    msg = f"content-factory: {len(written_files)} статей за {date_str}"
    run(
        ["git", "-c", "user.email=bot@example.com", "-c", "user.name=content-factory",
         "commit", "-m", msg],
        cwd=SITE_REPO_PATH,
    )
    run(["git", "push", "origin", SITE_REPO_BRANCH], cwd=SITE_REPO_PATH)
    return run(["git", "rev-parse", "HEAD"], cwd=SITE_REPO_PATH)


# ── Основной поток ────────────────────────────────────────────────────
def process_topic(topic: Topic, publish_date: str, blog_dir: Path) -> ArticleResult:
    res = ArticleResult(topic=topic)
    t0 = time.monotonic()
    try:
        log.info("→ Тема: %s", topic.topic)
        brief = make_seo_brief(topic, publish_date)
        res.slug = brief["slug"]
        res.title = brief["title"]

        # на случай если slug всё-таки совпал — добавим суффикс даты
        target = blog_dir / f"{res.slug}.mdx"
        if target.exists():
            res.slug = f"{res.slug}-{publish_date}"
            target = blog_dir / f"{res.slug}.mdx"
            brief["slug"] = res.slug

        mdx = make_article_mdx(brief, publish_date)
        if ENABLE_EDITOR_PASS:
            mdx = edit_article_mdx(mdx)

        finalize_article(res, brief, mdx, blog_dir, publish_date)
        log.info("✓ %s → %s (%s)", topic.primary_keyword, res.file_path.name, res.status)
    except Exception as e:  # noqa: BLE001
        log.exception("✗ Ошибка на теме %s", topic.topic)
        res.status = "failed"
        res.error = str(e)
    finally:
        res.elapsed_sec = round(time.monotonic() - t0, 2)
    return res


def finalize_article(res: ArticleResult, brief: dict, mdx: str,
                     blog_dir: Path, publish_date: str) -> None:
    """Постобработка сгенерированной статьи: нормализация slug, чистка якорей,
    запись файла, выставление статуса, сохранение debug-брифа. Общая для обоих режимов.
    """
    # нормализуем slug в frontmatter (на случай если редактор переписал)
    mdx = ensure_frontmatter_slug(mdx, res.slug)

    # убираем протекающие {#anchor} из H2–H6 (LLM игнорит запрет в промпте)
    mdx, anchors_stripped = strip_heading_anchors(mdx)
    if anchors_stripped:
        log.warning("  ⚠ %s: убрано %d протекающих {#anchor} из заголовков", res.slug, anchors_stripped)

    target = blog_dir / f"{res.slug}.mdx"
    target.write_text(mdx, encoding="utf-8")
    res.file_path = target
    res.status = "draft" if "\ndraft: true" in mdx else "published"

    debug_path = LOGS / f"brief-{publish_date}-{res.slug}.json"
    debug_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")


def generate_articles_batched(topics: list[Topic], publish_date: str,
                              blog_dir: Path) -> list[ArticleResult]:
    """Стадийная генерация через Batch API: 5 брифов → 5 текстов → 5 редактур.
    Каждая стадия — один батч (−50% к цене). Статьи независимы, потому батчатся
    по этапам; шаги одной статьи остаются последовательными между батчами.
    Сбой отдельной статьи на любой стадии не валит остальные.
    """
    results: list[ArticleResult] = []
    # i (индекс темы) — стабильный custom_id через все стадии.
    states: dict[int, dict] = {}

    # ── Стадия 1: брифы
    briefs_raw = run_stage_batch(
        "brief", read_prompt("01_seo_brief.md"),
        [(f"a{i}", brief_user(t, publish_date)) for i, t in enumerate(topics)],
        max_tokens=4000,
    )
    for i, topic in enumerate(topics):
        res = ArticleResult(topic=topic)
        raw = briefs_raw.get(f"a{i}")
        if not raw:
            res.status, res.error = "failed", "бриф не сгенерирован (batch)"
            results.append(res)
            continue
        try:
            brief = parse_brief(raw)
            res.slug = brief["slug"]
            res.title = brief["title"]
        except Exception as e:  # noqa: BLE001
            res.status, res.error = "failed", f"бриф не распарсился: {e}"
            results.append(res)
            continue
        # коллизия slug → суффикс даты
        if (blog_dir / f"{res.slug}.mdx").exists():
            res.slug = f"{res.slug}-{publish_date}"
            brief["slug"] = res.slug
        states[i] = {"topic": topic, "res": res, "brief": brief}

    # ── Стадия 2: тексты
    arts_raw = run_stage_batch(
        "article", read_prompt("02_article_writer.md"),
        [(f"a{i}", article_user(s["brief"], publish_date)) for i, s in states.items()],
        max_tokens=8000,
    )
    for i in list(states.keys()):
        raw = arts_raw.get(f"a{i}")
        if not raw:
            s = states.pop(i)
            s["res"].status, s["res"].error = "failed", "текст не сгенерирован (batch)"
            results.append(s["res"])
            continue
        states[i]["mdx"] = raw.strip()

    # ── Стадия 3: редактура (опционально)
    if ENABLE_EDITOR_PASS and states:
        eds_raw = run_stage_batch(
            "edit", read_prompt("03_editor_factcheck.md"),
            [(f"a{i}", edit_user(s["mdx"])) for i, s in states.items()],
            max_tokens=8000,
        )
        for i, s in states.items():
            edited = eds_raw.get(f"a{i}")
            if edited:
                s["mdx"] = edited.strip()
            else:
                log.warning("Batch[edit]: a%d не отредактирована — беру нередактированную версию", i)

    # ── Финализация
    for i, s in states.items():
        res = s["res"]
        try:
            finalize_article(res, s["brief"], s["mdx"], blog_dir, publish_date)
            log.info("✓ %s → %s (%s)", res.topic.primary_keyword, res.file_path.name, res.status)
        except Exception as e:  # noqa: BLE001
            log.exception("✗ Финализация провалилась: %s", res.topic.topic)
            res.status, res.error = "failed", str(e)
        results.append(res)

    # возвращаем в исходном порядке тем
    results.sort(key=lambda r: topics.index(r.topic))
    return results


def ensure_frontmatter_slug(mdx: str, slug: str) -> str:
    """На всякий случай — приколачиваем slug к frontmatter, если редактор его сбил."""
    pattern = re.compile(r"^slug:\s*\".*\"", re.MULTILINE)
    if pattern.search(mdx):
        return pattern.sub(f'slug: "{slug}"', mdx, count=1)
    # если slug отсутствует в frontmatter — это баг этапа 2, но не валим, добавим
    if mdx.startswith("---"):
        return mdx.replace("---", f'---\nslug: "{slug}"', 1)
    return mdx


HEADING_ANCHOR_RE = re.compile(r"^(#{2,6}.*?)\s*\{#[a-z0-9-]+\}\s*$", re.MULTILINE)


def strip_heading_anchors(mdx: str) -> tuple[str, int]:
    """Убирает протекающие `{#anchor}` из строк заголовков H2–H6.

    LLM в фабрике игнорирует запрет в 02_article_writer.md и периодически
    дописывает `## Заголовок {#anchor-id}`. Наш рендер использует marked
    (не MDX/remark), такого синтаксиса не понимает — литерал протекает в HTML.
    ID заголовкам и так автоматически назначаются через assignTocIds
    в src/components/blog/article-body.tsx из массива toc во frontmatter.

    Возвращает (очищенный mdx, число удалённых якорей).
    """
    cleaned, n = HEADING_ANCHOR_RE.subn(r"\1", mdx)
    return cleaned, n


def main() -> int:
    started = datetime.now(timezone.utc)
    publish_date = datetime.now().strftime("%Y-%m-%d")
    report = RunReport(started_at=started)

    log.info("=== content-factory: запуск %s ===", publish_date)

    try:
        ensure_site_repo()
    except subprocess.CalledProcessError as e:
        log.exception("Не удалось подготовить site-репо")
        send_report(
            subject=f"[content-factory] FAIL git: {publish_date}",
            html=f"<p>Не удалось склонировать/обновить college-site-ast.</p><pre>{e}</pre>",
        )
        return 2

    blog_dir = SITE_REPO_PATH / SITE_BLOG_DIR
    blog_dir.mkdir(parents=True, exist_ok=True)

    topics = pick_topics(ARTICLES_PER_DAY)
    if not topics:
        log.warning("Backlog пуст — заполните data/topics_backlog.csv")
        send_report(
            subject=f"[content-factory] backlog empty {publish_date}",
            html="<p>В topics_backlog.csv нет неиспользованных тем. Пополните пул.</p>",
        )
        return 1

    log.info("Выбрано %d тем", len(topics))

    successful_topics: list[Topic] = []
    used_rows: list[dict] = []
    written_files: list[Path] = []

    if USE_BATCH:
        try:
            results = generate_articles_batched(topics, publish_date, blog_dir)
        except Exception:  # noqa: BLE001
            log.exception("Batch-режим упал целиком — откатываюсь на последовательный")
            results = [process_topic(t, publish_date, blog_dir) for t in topics]
    else:
        results = [process_topic(t, publish_date, blog_dir) for t in topics]

    for res in results:
        report.results.append(res)
        if res.status in ("published", "draft") and res.file_path:
            written_files.append(res.file_path)
            successful_topics.append(res.topic)
            used_rows.append({
                "date": publish_date,
                "slug": res.slug,
                "title": res.title,
                "primary_keyword": res.topic.primary_keyword,
                "category": res.topic.category,
                "status": res.status,
                "url": f"https://example.com/blog/{res.slug}",
            })

    # Сначала обновляем CSV (backlog/used) — они должны попасть в тот же коммит,
    # что и MDX-статьи. Если push потом провалится — откатим CSV-изменения через
    # git checkout, чтобы темы не пропали из backlog.
    if successful_topics:
        append_used(used_rows)
        remove_topics_from_backlog(successful_topics)

    # коммит и push
    push_ok = False
    if written_files:
        try:
            sha = commit_and_push(written_files, publish_date)
            report.commit_sha = sha
            push_ok = True
        except subprocess.CalledProcessError:
            log.exception("Git push провалился — откатываю CSV-изменения, темы остаются в backlog")
            report.commit_sha = None
            push_ok = False
            # Откат CSV: у нас в репо backlog уменьшен и used дополнен — оба нужно вернуть.
            try:
                cf_data = "content-factory/data"
                run(["git", "checkout", "--", f"{cf_data}/topics_backlog.csv",
                     f"{cf_data}/topics_used.csv"], cwd=SITE_REPO_PATH)
                log.info("CSV откачены до состояния origin/main")
            except subprocess.CalledProcessError:
                log.exception("Не удалось откатить CSV — внимание, состояние backlog/used может быть некорректным")

    if successful_topics and not push_ok:
        log.warning(
            "%d тем сгенерировано локально, но push провалился. "
            "Файлы остались в %s, темы НЕ удалены из backlog. "
            "Следующий прогон попробует ещё раз.",
            len(successful_topics), SITE_REPO_PATH,
        )

    report.finished_at = datetime.now(timezone.utc)

    # email-отчёт
    send_report(
        subject=f"[content-factory] {publish_date}: {len(written_files)}/{len(topics)} статей",
        html=render_email_html(report, publish_date),
    )

    failed = sum(1 for r in report.results if r.status == "failed")
    return 0 if failed == 0 else 1


# ── Email HTML ────────────────────────────────────────────────────────
def render_email_html(report: RunReport, date_str: str) -> str:
    rows = []
    for r in report.results:
        status_color = {
            "published": "#0a7",
            "draft": "#d80",
            "failed": "#c33",
        }.get(r.status, "#666")
        url = f"https://example.com/blog/{r.slug}" if r.slug else ""
        url_link = f'<a href="{url}">{url}</a>' if url else "—"
        rows.append(f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #eee;">{r.topic.primary_keyword}</td>
          <td style="padding:8px;border-bottom:1px solid #eee;">{r.title or '—'}</td>
          <td style="padding:8px;border-bottom:1px solid #eee;color:{status_color};font-weight:600;">{r.status}</td>
          <td style="padding:8px;border-bottom:1px solid #eee;font-size:13px;">{url_link}</td>
          <td style="padding:8px;border-bottom:1px solid #eee;font-size:13px;color:#999;">{r.elapsed_sec}s</td>
        </tr>
        """)
        if r.error:
            rows.append(f"""
            <tr><td colspan="5" style="padding:6px 8px;background:#fff5f5;color:#c33;font-size:12px;">{r.error}</td></tr>
            """)

    sha_block = (
        f'<p style="color:#666;">Коммит: <code>{report.commit_sha[:7]}</code></p>'
        if report.commit_sha else
        '<p style="color:#c33;">⚠ Коммит/push не выполнен — проверьте логи на VPS.</p>'
    )

    return f"""
    <html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:760px;margin:0 auto;padding:24px;">
      <h2 style="margin:0 0 8px;">Контент-фабрика — {date_str}</h2>
      <p style="color:#666;margin:0 0 16px;">
        Сгенерировано: {len(report.results)} •
        Опубликовано: {sum(1 for r in report.results if r.status == 'published')} •
        В черновиках: {sum(1 for r in report.results if r.status == 'draft')} •
        Ошибок: {sum(1 for r in report.results if r.status == 'failed')}
      </p>
      {sha_block}
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <thead>
          <tr style="background:#fafafa;text-align:left;">
            <th style="padding:8px;border-bottom:2px solid #ddd;">Ключ</th>
            <th style="padding:8px;border-bottom:2px solid #ddd;">Title</th>
            <th style="padding:8px;border-bottom:2px solid #ddd;">Статус</th>
            <th style="padding:8px;border-bottom:2px solid #ddd;">URL</th>
            <th style="padding:8px;border-bottom:2px solid #ddd;">⏱</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
      <p style="color:#999;margin-top:24px;font-size:12px;">
        Сайт пересобирается автоматически (webhook). Если статья ушла в <b>draft: true</b> — её статус в frontmatter,
        она закоммичена, но не показывается. Откройте файл в репо, проверьте, переключите draft на false и пушните.
      </p>
    </body></html>
    """


if __name__ == "__main__":
    sys.exit(main())
