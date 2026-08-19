#!/usr/bin/env bash
#
# Ставит скиллы завода в ~/.claude/skills/ — чтобы Claude Code подхватывал их
# в ЛЮБОЙ папке, а не только внутри этого репозитория.
#
# Что ставит (лежит прямо здесь, в .claude/skills/):
#   anti-neuroslop  — стоп-лист «нейрослопа»: что не делать при вёрстке страниц
#   conversion-ux   — правила конверсионного UX: как страница приносит заявки
#
# Запуск из корня репозитория:
#   ./install-skills.sh
#
# Уже стоящий скилл с тем же именем будет перезаписан — рядом останется бэкап
# с датой, чтобы можно было откатиться.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)/.claude/skills"
DST="${HOME}/.claude/skills"

[ -d "$SRC" ] || { echo "✗ не нашёл $SRC — запускай из корня репозитория" >&2; exit 1; }
mkdir -p "$DST"

stamp="$(date +%Y%m%d-%H%M%S)"
installed=0

for dir in "$SRC"/*/; do
  name="$(basename "$dir")"
  target="$DST/$name"
  if [ -d "$target" ]; then
    mv "$target" "$target.bak.$stamp"
    echo "  ~ $name — старая версия сохранена в $name.bak.$stamp"
  fi
  cp -R "$dir" "$target"
  echo "  ✓ $name"
  installed=$((installed + 1))
done

echo
echo "Готово: скиллов установлено — $installed → $DST"
echo "Проверить: открой Claude Code и набери / — они будут в списке."
echo
echo "Скиллы подхватываются при СТАРТЕ сессии: если Claude Code уже открыт, перезапусти."
