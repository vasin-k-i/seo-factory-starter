"""
IndexNow ping — мгновенное уведомление Яндекса и Bing о новых/обновлённых URL.

Использование:
    from modules.indexnow import ping_indexnow

    result = ping_indexnow([
        "https://example.com/blog/zarplata-vospitatelya-2026",
        "https://example.com/blog/spo-ili-vo-dlya-vospitatelya",
    ])

Ключ:
    Лежит в публичном файле public/<key>.txt. По умолчанию читается из ENV INDEXNOW_KEY
    либо берётся захардкоженный (см. INDEXNOW_KEY ниже — это файл в public/).

Спецификация: https://www.indexnow.org/documentation
Эндпоинты:
  - https://yandex.com/indexnow  — официальный приёмник Яндекса
  - https://api.indexnow.org/indexnow — общий эндпоинт (Яндекс + Bing разделяют ключи между собой)
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

import requests

log = logging.getLogger(__name__)

# Совпадает с именем файла в public/.
DEFAULT_KEY = ""  # задай свой ключ IndexNow через ENV INDEXNOW_KEY (файл public/<key>.txt)
DEFAULT_HOST = "example.com"
ENDPOINT = "https://yandex.com/indexnow"
# Максимум по спецификации — 10 000 URL на батч. Делаем меньше, чтобы был запас.
MAX_PER_BATCH = 1000


def _key() -> str:
    return (os.environ.get("INDEXNOW_KEY") or DEFAULT_KEY).strip()


def _host() -> str:
    return (os.environ.get("INDEXNOW_HOST") or DEFAULT_HOST).strip()


def _key_location() -> str:
    key = _key()
    return f"https://{_host()}/{key}.txt"


def ping_indexnow(urls: Iterable[str], *, dry_run: bool = False) -> dict:
    """Отправить пачку URL в IndexNow.

    Возвращает dict: { total, batches, ok, failed, responses: [{status, count, body?}] }.
    Статусы по спецификации:
      200 — приняты
      202 — приняты, но ключ ещё валидируется
      400 — bad request (формат)
      403 — ключ не валидируется
      422 — URL не принадлежат домену
      429 — слишком часто
    """
    url_list = [u.strip() for u in urls if u and u.strip()]
    # Удаляем дубли, сохраняя порядок.
    seen: set[str] = set()
    deduped: list[str] = []
    for u in url_list:
        if u not in seen:
            seen.add(u)
            deduped.append(u)

    host = _host()
    # Защита: пинговать только наши URL.
    foreign = [u for u in deduped if host not in u]
    if foreign:
        log.warning("ping_indexnow: %d URL не принадлежат %s — отбрасываю: %s",
                    len(foreign), host, foreign[:3])
    deduped = [u for u in deduped if host in u]

    result = {
        "total": len(deduped),
        "batches": 0,
        "ok": 0,
        "failed": 0,
        "responses": [],
    }
    if not deduped:
        return result

    key = _key()
    key_loc = _key_location()

    for i in range(0, len(deduped), MAX_PER_BATCH):
        batch = deduped[i:i + MAX_PER_BATCH]
        payload = {
            "host": host,
            "key": key,
            "keyLocation": key_loc,
            "urlList": batch,
        }
        result["batches"] += 1
        if dry_run:
            log.info("DRY-RUN ping_indexnow batch=%d count=%d", result["batches"], len(batch))
            result["responses"].append({"status": 0, "count": len(batch), "dry_run": True})
            result["ok"] += 1
            continue
        try:
            response = requests.post(
                ENDPOINT,
                json=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=15,
            )
            entry = {"status": response.status_code, "count": len(batch)}
            if response.status_code >= 400:
                entry["body"] = response.text[:500]
                result["failed"] += 1
                log.error("IndexNow batch %d failed: HTTP %d — %s",
                          result["batches"], response.status_code, response.text[:200])
            else:
                result["ok"] += 1
                log.info("IndexNow batch %d ok: HTTP %d, %d URLs",
                         result["batches"], response.status_code, len(batch))
            result["responses"].append(entry)
        except requests.RequestException as e:
            result["failed"] += 1
            result["responses"].append({"status": -1, "count": len(batch), "error": str(e)})
            log.error("IndexNow batch %d exception: %s", result["batches"], e)

    return result


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    urls = sys.argv[1:] if len(sys.argv) > 1 else [
        "https://example.com/",
        "https://example.com/blog",
    ]
    print(f"→ Пинг {len(urls)} URL через {ENDPOINT}")
    print(f"  keyLocation = {_key_location()}")
    r = ping_indexnow(urls)
    print(f"  total={r['total']} batches={r['batches']} ok={r['ok']} failed={r['failed']}")
    for resp in r["responses"]:
        print(f"    {resp}")
