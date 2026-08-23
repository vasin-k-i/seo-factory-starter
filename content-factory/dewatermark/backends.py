#!/usr/bin/env python3
"""Модели пайплайна: сменные рерайтеры и аудитор.

Разделение ролей жёсткое и принципиальное:

  РЕРАЙТЕР — любая модель, КРОМЕ Claude. Он единственный, кто пишет публикуемый текст.
  Claude здесь запрещён: его правка вернёт маркировку и обнулит весь прогон.

  АУДИТОР — Claude. Он только ЧИТАЕТ и возвращает список дефектов. В текст статьи
  не попадает ни один его токен, поэтому маркировку он не приносит.

Рерайтеры сменные: deepseek (проверен), codex, grok (проверен, но течёт служебкой).
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import tempfile
import urllib.request

HOME = pathlib.Path.home()


def _find(name: str, *fallbacks: str) -> str:
    """CLI ищем в PATH, потом по типовым путям установки, потом по env-переменной."""
    from shutil import which
    env = os.environ.get(f"{name.upper()}_CLI_PATH")
    if env and pathlib.Path(env).exists():
        return env
    found = which(name)
    if found:
        return found
    for f in fallbacks:
        if pathlib.Path(f).expanduser().exists():
            return str(pathlib.Path(f).expanduser())
    raise RuntimeError(
        f"{name} CLI не найден. Поставьте его или укажите путь в {name.upper()}_CLI_PATH")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
# ⚠️ НЕ reasoning-модель: на `deepseek-v4-flash` весь max_tokens уходит в
# reasoning_content, content приходит пустой, а прогон молча отдаёт оригинал.
DEEPSEEK_MODEL = "deepseek-chat"

# $/1M токенов, DeepSeek V4-Flash после репрайса 16.08.2026, тариф peak (пессимистично).
PRICE_CACHED, PRICE_IN, PRICE_OUT = 0.014, 0.44, 1.32
USD_RUB = 80.0

USAGE = {"prompt": 0, "cache_hit": 0, "completion": 0, "calls": 0}


def reset_usage() -> None:
    for k in USAGE:
        USAGE[k] = 0


def cost_rub() -> float:
    return USD_RUB * (USAGE["prompt"] / 1e6 * PRICE_IN
                      + USAGE["cache_hit"] / 1e6 * PRICE_CACHED
                      + USAGE["completion"] / 1e6 * PRICE_OUT)


# ── DeepSeek ──────────────────────────────────────────────────────────────────
def _deepseek_key() -> str:
    """Ключ ищем по цепочке источников: env → центральный стор → .env сайтов."""
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    candidates = [pathlib.Path(".env"), pathlib.Path("content-factory/.env"),
                  pathlib.Path("seo-agent/.env"), HOME / ".config/dewatermark.env"]
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key:
                    return key
    raise RuntimeError(
        "нет DEEPSEEK_API_KEY: ни в окружении, ни в "
        + ", ".join(str(p) for p in candidates))


def call_deepseek(system: str, user: str) -> str:
    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.4,
        "max_tokens": 4000,
    }).encode()
    req = urllib.request.Request(
        DEEPSEEK_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_deepseek_key()}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read().decode())

    u = data.get("usage") or {}
    USAGE["calls"] += 1
    USAGE["cache_hit"] += u.get("prompt_cache_hit_tokens", 0)
    USAGE["prompt"] += u.get("prompt_cache_miss_tokens", u.get("prompt_tokens", 0))
    USAGE["completion"] += u.get("completion_tokens", 0)

    choice = data["choices"][0]
    content = (choice["message"].get("content") or "").strip()
    if not content:
        raise RuntimeError(
            f"пустой content, finish_reason={choice.get('finish_reason')} — "
            f"похоже, {DEEPSEEK_MODEL} стала reasoning-моделью")
    return content


# ── Codex ─────────────────────────────────────────────────────────────────────
def _codex_bin() -> str:
    # ⚠️ Бэкенд написан по документации codex exec, но вживую НЕ обкатан:
    # на машине автора codex CLI не установлен. Проверьте на одной статье
    # с --dry, прежде чем ставить его в поток.
    return _find("codex", "~/.codex/bin/codex")


def call_codex(system: str, user: str) -> str:
    """codex exec болтает в stdout — ответ забираем через --output-last-message."""
    with tempfile.NamedTemporaryFile(mode="r", suffix=".txt", delete=False) as tf:
        out_path = tf.name
    try:
        r = subprocess.run(
            [_codex_bin(), "exec", "--skip-git-repo-check",
             "--output-last-message", out_path, "-"],
            input=f"[Системная инструкция]\n{system}\n\n[Задача]\n{user}",
            capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise RuntimeError(f"codex rc={r.returncode}: {(r.stderr or r.stdout)[:300]}")
        return pathlib.Path(out_path).read_text().strip()
    finally:
        pathlib.Path(out_path).unlink(missing_ok=True)


# ── Grok ──────────────────────────────────────────────────────────────────────
# Агентский CLI: проговаривает план вслух и клеит его к ответу без отбивки —
# «…объём около 117 слов.Сверю объём…У гигтеста». В статью это уходит как текст,
# вместе с выдуманными числами из служебных фраз. Течёт и в середину блока.
CHATTER = re.compile(
    r"совпадени|цепочк|исходник|черновик|пят[иь] слов|первой версии|"
    r"перепишу|перепишем|сверю|проверяю объём|проверю объём|ломаю|"
    r"по правилам:|тот же смысл и разметка", re.I)


def clean_chatter(text: str) -> str:
    out = []
    for block in text.split("\n\n"):
        if block.lstrip().startswith(("|", "#", "-", "*", "<")) or not CHATTER.search(block):
            out.append(block)
            continue
        parts = re.split(r"(?<=[.!?])\s*(?=[А-ЯЁA-Z])", block)
        kept = [s for s in parts if not CHATTER.search(s)]
        out.append(" ".join(s.strip() for s in kept).strip())
    return "\n\n".join(b for b in out if b.strip()).strip()


def call_grok(system: str, user: str) -> str:
    r = subprocess.run([_find("grok", "~/.grok/bin/grok"), "-p", f"{system}\n\n{user}"],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"grok rc={r.returncode}: {(r.stderr or r.stdout)[:300]}")
    return clean_chatter(r.stdout.strip())


# ── Аудитор: только Claude и только на чтение ─────────────────────────────────
def call_claude(prompt: str, model: str = "sonnet") -> str:
    # ключ из окружения вычищаем: иначе CLI уходит биллиться по API мимо подписки
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["PATH"] = f"{HOME}/.local/bin:/opt/homebrew/bin:/usr/bin:/bin"
    r = subprocess.run(
        [_find("claude", "~/.local/bin/claude"), "-p", "--output-format", "text", "--model", model],
        input=prompt, capture_output=True, text=True, env=env, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"claude rc={r.returncode}: {(r.stderr or r.stdout)[:300]}")
    return r.stdout.strip()


REWRITERS = {"deepseek": call_deepseek, "codex": call_codex, "grok": call_grok}


def strip_fence(text: str) -> str:
    text = re.sub(r"^```[a-z]*\s*\n", "", text.strip())
    return re.sub(r"\n```\s*$", "", text).strip()


def parse_json(text: str) -> dict | list:
    """Модели любят обернуть JSON в прозу — достаём первый валидный объект/массив."""
    text = strip_fence(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"[\[{].*[\]}]", text, re.S)
    if not m:
        raise ValueError(f"в ответе нет JSON: {text[:200]}")
    return json.loads(m.group(0))
