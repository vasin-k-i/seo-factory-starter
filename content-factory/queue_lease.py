#!/usr/bin/env python3
"""Файловый лок на очередь тем — чтобы два прогона не взяли одни и те же темы.

Зачем: очередь фабрики — это два CSV (`data/topics_backlog.csv` и
`data/topics_used.csv`). Прогон читает бэклог в начале и переписывает оба файла
в конце. Два прогона внахлёст (ручной запуск поверх крона, или вчерашний ещё не
закончился) прочитают ОДИН И ТОТ ЖЕ бэклог, сгенерируют одни и те же статьи и
затрут пометки друг друга. Деньги за вторую копию каждой статьи — впустую.

Семантика: advisory-лок flock(LOCK_EX), по умолчанию НЕблокирующий. Занято —
yield False, вызывающий код решает сам (обычно тихо выйти). Освобождается
автоматически при закрытии дескриптора и при падении процесса (ядро снимает
flock вместе с процессом — «протухших» локов не бывает).

    with queue_lease.acquire(LOCK_PATH) as acquired:
        if not acquired:
            print("другой прогон уже идёт — выхожу")
            return 0
        ...

⚠️ flock работает МЕЖДУ процессами. Внутри одного процесса второй захват того
же файла успешен (это свойство flock, не баг) — проверять надо subprocess'ом.

⚠️ POSIX-only (Linux, macOS): модуль fcntl на Windows отсутствует.
"""
from __future__ import annotations

import contextlib
import fcntl
from pathlib import Path


@contextlib.contextmanager
def acquire(lock_path, blocking: bool = False):
    """Эксклюзивный файловый лок. yield True — захвачен, False — занят другим процессом."""
    p = Path(lock_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "w")
    locked = False
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(f, flags)
            locked = True
        except OSError:
            # BlockingIOError (подкласс OSError) — занято. На части платформ
            # прилетает EACCES вместо EWOULDBLOCK, ловим OSError целиком.
            locked = False
        yield locked
    finally:
        if locked:
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()
