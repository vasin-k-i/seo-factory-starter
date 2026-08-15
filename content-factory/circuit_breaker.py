#!/usr/bin/env python3
"""Предохранитель от рантвея контент-фабрики — превентивная страховка.

Это НЕ фикс существующего бага: фабрика сейчас ведёт себя смирно (повторы
вызова Claude ограничены тремя попытками, темы вычёркиваются из бэклога после
публикации). Модуль — на случай, когда цикл начнёт крутиться сам на себя:
залипший retry, который жжёт токены; прогон, который вдруг трогает полсотни
файлов; автоматика, полезшая в путь, который трогать нельзя.

Счётчики живут в data/circuit_breaker.json и сбрасываются на новый день (новый
день = новое состояние). Все функции возвращают True = «порог превышен, прогон
пора остановить» — решение принимает вызывающий код, модуль сам ничего не
останавливает и исключений не кидает.

Пороги — константы ниже, правятся руками.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "data" / "circuit_breaker.json"

# ─────────────────────────── пороги ───────────────────────────
MAX_RETRY_LOOP = 20             # повторов вызова LLM за сутки на всю фабрику
MAX_FILES_CHANGED_PER_RUN = 50  # файлов, изменённых автоматикой за сутки
PROTECTED_PATHS: list[str] = []  # заполнить при необходимости: префиксы путей,
                                 # которые автоматика не имеет права трогать


def _today() -> str:
    return datetime.now().date().isoformat()


def _blank(date: str | None = None) -> dict:
    return {"date": date or _today(), "retry_count": 0,
            "files_changed": 0, "protected_path_hits": 0}


def _load() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError):
        return _blank()
    if state.get("date") != _today():
        return _blank()
    base = _blank(state["date"])
    base.update({k: state.get(k, 0) for k in ("retry_count", "files_changed",
                                              "protected_path_hits")})
    return base


def _save(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def reset_if_new_day() -> dict:
    """Сверяет дату в файле состояния с сегодняшней, при расхождении обнуляет счётчики."""
    state = _load()
    _save(state)
    return state


def record_retry() -> bool:
    """+1 к счётчику повторов. True = порог MAX_RETRY_LOOP превышен, прогон надо остановить."""
    state = _load()
    state["retry_count"] += 1
    _save(state)
    return state["retry_count"] > MAX_RETRY_LOOP


def record_file_changed(path: str) -> bool:
    """+1 к счётчику изменённых файлов (+ проверка защищённых путей).

    True = превышен MAX_FILES_CHANGED_PER_RUN ИЛИ путь попал в PROTECTED_PATHS.
    """
    state = _load()
    state["files_changed"] += 1
    protected = any(str(path).startswith(p) for p in PROTECTED_PATHS)
    if protected:
        state["protected_path_hits"] += 1
    _save(state)
    return protected or state["files_changed"] > MAX_FILES_CHANGED_PER_RUN


def status() -> dict:
    """Текущее состояние + пороги (для логов/панели)."""
    state = _load()
    state["limits"] = {"max_retry_loop": MAX_RETRY_LOOP,
                       "max_files_changed_per_run": MAX_FILES_CHANGED_PER_RUN,
                       "protected_paths": list(PROTECTED_PATHS)}
    return state


if __name__ == "__main__":
    print(json.dumps(status(), ensure_ascii=False, indent=2))
