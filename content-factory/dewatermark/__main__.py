#!/usr/bin/env python3
"""CLI пайплайна стирания маркировки.

    python3 -m dewatermark --dir content-factory/queue --limit 5
    python3 -m dewatermark --file content/blog/statya.mdx --dry
    python3 -m dewatermark --dir content/blog --new-only    # только свежие, по git

⚠️ Запускать ТОЛЬКО если статьи пишет Claude. Если у вас в `models.writer.backend`
стоит не Claude — маркировки в тексте нет, и рерайт будет чистой тратой денег.
Проверить: `--check-writer path/to/site.config.yml`.

Формат берётся по расширению: .mdx / .md / .html.
Обработанное помечается в `<папка>/.dewatermark.json` по хешу содержимого —
повторный запуск не гоняет одно и то же дважды.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys

from . import pipeline

STATE_NAME = ".dewatermark.json"
ARTICLE_SUFFIXES = {".mdx", ".md", ".html"}
MARKING_BACKENDS = {"claude", "claude-cli", "anthropic"}


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _load_state(d: pathlib.Path) -> dict:
    p = d / STATE_NAME
    return json.loads(p.read_text()) if p.exists() else {}


def _save_state(d: pathlib.Path, state: dict) -> None:
    (d / STATE_NAME).write_text(json.dumps(state, ensure_ascii=False, indent=1))


def _git_fresh(d: pathlib.Path) -> set[str]:
    r = subprocess.run(["git", "status", "--porcelain", str(d)],
                       capture_output=True, text=True, cwd=str(d))
    return {pathlib.Path(ln[3:].strip().strip('"')).name for ln in r.stdout.splitlines()}


def check_writer(config_path: pathlib.Path) -> int:
    """Главный выключатель: нужен ли этому сайту пайплайн вообще."""
    if not config_path.exists():
        print(f"нет файла {config_path}")
        return 1
    m = re.search(r"^\s*writer:\s*\{[^}]*backend:\s*[\"']?([\w-]+)",
                  config_path.read_text(encoding="utf-8"), re.M)
    writer = m.group(1) if m else "unknown"
    needed = writer.lower() in MARKING_BACKENDS
    print(f"writer.backend = {writer}")
    if needed:
        print("→ пайплайн НУЖЕН: этот писатель ставит маркировку в текст.")
    else:
        print("→ пайплайн НЕ нужен: маркировку этот писатель не ставит.")
        print("  Запускать рерайт — платить за воздух.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="dewatermark")
    ap.add_argument("--dir", type=pathlib.Path, help="папка со статьями (очередь или content/blog)")
    ap.add_argument("--file", type=pathlib.Path, help="одна статья")
    ap.add_argument("--check-writer", type=pathlib.Path, metavar="site.config.yml")
    ap.add_argument("--rewriter", default="deepseek", choices=("deepseek", "codex", "grok"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--new-only", action="store_true",
                    help="только незакоммиченные/изменённые — для фабрик без очереди")
    ap.add_argument("--dry", action="store_true", help="не писать результат на диск")
    ap.add_argument("--report", type=pathlib.Path)
    a = ap.parse_args()

    if a.check_writer:
        return check_writer(a.check_writer)

    if a.file:
        results = [pipeline.process(a.file, rewriter_name=a.rewriter, dry=a.dry)]
        target = None
    elif a.dir:
        if not a.dir.is_dir():
            print(f"нет папки {a.dir}")
            return 1
        state = _load_state(a.dir)
        fresh = _git_fresh(a.dir) if a.new_only else None
        files = [p for p in sorted(a.dir.iterdir())
                 if p.suffix.lower() in ARTICLE_SUFFIXES and not p.name.startswith(".")
                 and (fresh is None or p.name in fresh)
                 and state.get(p.name, {}).get("digest") != _digest(p)]
        files = files[:a.limit] if a.limit else files
        if not files:
            print("новых статей нет")
            return 0
        print(f"{len(files)} статей, рерайтер {a.rewriter}")
        results, target = [], a.dir
        for f in files:
            r = pipeline.process(f, rewriter_name=a.rewriter, dry=a.dry)
            results.append(r)
            if not a.dry:
                state[f.name] = {"digest": _digest(f), "status": r.status,
                                 "critical": r.critical}
                _save_state(a.dir, state)
    else:
        ap.error("нужен --dir, --file или --check-writer")

    ok = sum(1 for r in results if r.status == "published")
    bad = [r for r in results if r.status != "published"]
    print(f"\nИТОГ: принято {ok}/{len(results)}, "
          f"потрачено {sum(r.cost_rub for r in results):.2f} ₽")
    for r in bad:
        print(f"  ✗ {pathlib.Path(r.path).name}: {r.status} — "
              + ("; ".join(r.critical) or r.reason))

    if a.report:
        a.report.write_text(pipeline.to_json(results), encoding="utf-8")
        print(f"отчёт → {a.report}")
    return 0 if not bad else 2


if __name__ == "__main__":
    sys.exit(main())
