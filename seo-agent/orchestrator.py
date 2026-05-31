"""
seo-agent · orchestrator

Единая точка входа для запуска модулей M1–M6.

Запуск:
    python3 orchestrator.py m2                  # технический аудит
    python3 orchestrator.py m2 --dry-run        # без Telegram
    python3 orchestrator.py m2 --skip-psi       # без PageSpeed (отладка)

Все модули — независимые. CI/cron вызывает orchestrator с конкретным аргументом.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))


def cmd_m1(args: argparse.Namespace) -> int:
    from modules import m1_semantic
    m1_semantic.run_collection(days=args.days, dry_run=args.dry_run)
    return 0


def cmd_m2(args: argparse.Namespace) -> int:
    from modules import m2_audit
    m2_audit.run_audit(dry_run=args.dry_run, skip_psi=args.skip_psi)
    return 0


def cmd_m3(args: argparse.Namespace) -> int:
    from modules import m3_rankings
    m3_rankings.run_rankings(refresh_seeds=args.refresh_seeds, dry_run=args.dry_run)
    return 0


def cmd_m4(args: argparse.Namespace) -> int:
    from modules import m4_eeat
    m4_eeat.run_eeat(since=args.since, dry_run=args.dry_run, top_n_alert=args.top_n_issues)
    return 0


def cmd_m5(args: argparse.Namespace) -> int:
    from modules import m5_linker
    m5_linker.run_linker(dry_run=args.dry_run)
    return 0


def cmd_m6(args: argparse.Namespace) -> int:
    from modules import m6_competitors
    m6_competitors.run_competitors(dry_run=args.dry_run)
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    from modules import weekly_digest
    weekly_digest.run_digest(dry_run=args.dry_run)
    return 0


def cmd_daily(args: argparse.Namespace) -> int:
    from modules import daily_digest
    daily_digest.run_daily(dry_run=args.dry_run)
    return 0


def cmd_notimplemented(name: str):
    def _run(_args: argparse.Namespace) -> int:
        logging.error("Модуль %s ещё не реализован. См. docs/seo/agent-system/sprint-log.md", name)
        return 2
    return _run


COMMANDS = {
    "m1": cmd_m1,
    "m2": cmd_m2,
    "m3": cmd_m3,
    "m4": cmd_m4,
    "m5": cmd_m5,
    "m6": cmd_m6,
    "digest": cmd_digest,
    "daily": cmd_daily,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="seo-agent orchestrator")
    parser.add_argument("module", choices=sorted(COMMANDS.keys()), help="Модуль для запуска")
    parser.add_argument("--dry-run", action="store_true", help="Не отправлять в Telegram")
    parser.add_argument("--skip-psi", action="store_true", help="(M2) Пропустить PageSpeed-фазу")
    parser.add_argument("--days", type=int, default=30, help="(M1) Глубина истории query (дней)")
    parser.add_argument("--refresh-seeds", action="store_true",
                        help="(M3) Пересобрать список ключей из MDX + CSV")
    parser.add_argument("--since", type=str, default=None,
                        help="(M4) Сканировать статьи с этой даты (ISO YYYY-MM-DD)")
    parser.add_argument("--top-n-issues", type=int, default=5,
                        help="(M4) Сколько worst-articles обработать (открыть Issues)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    return COMMANDS[args.module](args)


if __name__ == "__main__":
    raise SystemExit(main())
