#!/usr/bin/env python3
"""Персистентный учёт расхода LLM — ТОЛЬКО учёт, без ограничений.

Зачем: у прогона фабрики есть цифры расхода (сколько токенов сожгла статья), но
живут они ровно до конца процесса. Через сутки узнать «сколько стоила неделя» и
«во что обходится одна статья» не из чего. Этот модуль дописывает каждый вызов
LLM строкой в JSONL-файл, который переживает прогон, — его читает и человек, и
панель/дашборд.

⚠️ Здесь СОЗНАТЕЛЬНО нет лимитов, порогов и остановки прогона. Только запись и
чтение (видимость расхода). Ограничивать бюджет — отдельное решение, не тут.

Зависимостей нет (только stdlib). JSONL аппендится построчно, файл может расти
сколько угодно — ротация не нужна и не делается.

Формат строки:
  {"ts": ISO8601, "site": "...", "model": ..., "backend": ...,
   "input_tokens": N, "output_tokens": N, "cache_read_tokens": N,
   "cache_creation_tokens": N, "cost_usd": float|null, "metered": bool,
   "run_id": str|null, "note": str}

`metered=false` — вызов, за который реальных предельных денег не списывают
(например, работа по подписке). Токены и число вызовов всё равно интересны,
поэтому в сводках две суммы: `cost_usd` (все) и `cost_usd_metered_only`
(только то, за что реально платим).

CLI: `python3 usage_ledger.py [дней]` — сводка за сегодня и за N дней (по умолчанию 7).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = os.environ.get("SITE_NAME") or "seo-factory"
LEDGER_PATH = HERE / "data" / "budget" / "usage.jsonl"

# ─────────────────────────── цены моделей ───────────────────────────
# $ за 1 млн токенов, официальный прайс Anthropic API (сверено 2026-08-15,
# platform.claude.com/docs/en/pricing). Ключ — префикс имени модели.
# Не нашли модель в таблице → пишем cost_usd=None: пусть лучше в леджере будет
# честный «не знаю», чем выдуманная цифра. Токены записываются всегда.
_PRICES: dict[str, tuple[float, float]] = {
    # префикс модели: (вход, выход)
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-4": (5.00, 25.00),
    "claude-opus-5": (5.00, 25.00),
}

# Множители к цене входного токена (те же для всех моделей):
_CACHE_READ_MULT = 0.10   # чтение из кэша — примерно 1/10 цены
_CACHE_WRITE_MULT = 1.25  # запись в кэш (ephemeral, 5 мин) — дороже обычного входа
_BATCH_DISCOUNT = 0.50    # Batch API — половина цены на всё


def estimate_cost(model: str,
                  input_tokens: int,
                  output_tokens: int,
                  cache_read_tokens: int = 0,
                  cache_creation_tokens: int = 0,
                  batch: bool = False) -> float | None:
    """Считает стоимость вызова в долларах. None — если цена модели неизвестна.

    `input_tokens` — это НЕкэшированный вход: в ответе Anthropic кэш-токены
    лежат в отдельных полях и в input_tokens уже не входят, поэтому здесь всё
    складывается, а не вычитается.
    """
    price = next((p for prefix, p in _PRICES.items() if model.startswith(prefix)), None)
    if price is None:
        return None
    price_in, price_out = price
    usd = (
        int(input_tokens or 0) * price_in
        + int(cache_read_tokens or 0) * price_in * _CACHE_READ_MULT
        + int(cache_creation_tokens or 0) * price_in * _CACHE_WRITE_MULT
        + int(output_tokens or 0) * price_out
    ) / 1_000_000
    return usd * _BATCH_DISCOUNT if batch else usd


def record(model: str,
           backend: str,
           input_tokens: int,
           output_tokens: int,
           cache_read_tokens: int = 0,
           cache_creation_tokens: int = 0,
           cost_usd: float | None = None,
           metered: bool = True,
           run_id: str | None = None,
           note: str = "") -> None:
    """Дописывает один вызов LLM в usage.jsonl.

    Никогда не роняет вызывающий код: учёт не должен ломать генерацию.
    """
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "site": SITE,
        "model": model,
        "backend": backend,
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "cache_read_tokens": int(cache_read_tokens or 0),
        "cache_creation_tokens": int(cache_creation_tokens or 0),
        "cost_usd": float(cost_usd) if cost_usd is not None else None,
        "metered": bool(metered),
        "run_id": run_id,
        "note": note,
    }
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        # Диск полон / прав нет — учёт молча пропускаем, прогон продолжается.
        pass


def record_response(model: str,
                    usage,
                    backend: str = "anthropic-api",
                    note: str = "",
                    metered: bool = True) -> None:
    """Обёртка: пишет расход прямо из объекта `response.usage` Anthropic SDK.

    Удобно там, где вызов LLM одиночный и разбирать поля вручную незачем
    (модули seo-agent). Стоимость считает сама, по таблице цен выше.
    """
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    c_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    c_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    record(model=model, backend=backend,
           input_tokens=inp, output_tokens=out,
           cache_read_tokens=c_read, cache_creation_tokens=c_write,
           cost_usd=estimate_cost(model, inp, out, c_read, c_write),
           metered=metered, note=note)


# ─────────────────────────── чтение / сводки ───────────────────────────

def _iter_rows():
    """Строки леджера. Битые строки пропускаем (файл дописывается конкурентно)."""
    if not LEDGER_PATH.exists():
        return
    with open(LEDGER_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _empty() -> dict:
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cost_usd": 0.0, "cost_usd_metered_only": 0.0}


def _aggregate(since_date) -> dict:
    """Сводка по строкам с датой >= since_date (объект date). None = без границы."""
    agg = _empty()
    for row in _iter_rows():
        ts = str(row.get("ts") or "")[:10]
        if since_date is not None:
            try:
                if datetime.strptime(ts, "%Y-%m-%d").date() < since_date:
                    continue
            except ValueError:
                continue
        agg["calls"] += 1
        agg["input_tokens"] += int(row.get("input_tokens") or 0)
        agg["output_tokens"] += int(row.get("output_tokens") or 0)
        cost = row.get("cost_usd") or 0.0
        agg["cost_usd"] += float(cost)
        if row.get("metered"):
            agg["cost_usd_metered_only"] += float(cost)
    agg["cost_usd"] = round(agg["cost_usd"], 6)
    agg["cost_usd_metered_only"] = round(agg["cost_usd_metered_only"], 6)
    return agg


def today_summary() -> dict:
    """Расход за сегодня. Нет файла → нули (не падаем)."""
    return _aggregate(datetime.now().date())


def summary(days: int) -> dict:
    """Расход за последние N дней (сегодня и предыдущие N-1 суток).

    days <= 0 → за всё время.
    """
    if days <= 0:
        return _aggregate(None)
    return _aggregate(datetime.now().date() - timedelta(days=days - 1))


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    print(f"леджер: {LEDGER_PATH} (есть: {LEDGER_PATH.exists()})")
    print("сегодня:", json.dumps(today_summary(), ensure_ascii=False))
    print(f"за {n} дн.:", json.dumps(summary(n), ensure_ascii=False))
