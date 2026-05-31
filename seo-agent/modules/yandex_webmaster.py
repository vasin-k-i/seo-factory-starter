"""
Яндекс.Вебмастер API client.

Использование:
    from modules.yandex_webmaster import yw_client, yw_get_user_id, yw_list_hosts, yw_search_queries

    user_id = yw_get_user_id()
    hosts = yw_list_hosts(user_id)
    queries = yw_search_queries(user_id, host_id, date_from="2026-05-13", date_to="2026-05-19")

ENV:
    YANDEX_WEBMASTER_TOKEN — OAuth access_token со scope webmaster:hostinfo,verify

Docs: https://yandex.ru/dev/webmaster/doc/dg/concepts/about.html
"""

from __future__ import annotations

import os
import logging
from typing import Optional
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.webmaster.yandex.net/v4"


def _token() -> str:
    t = os.environ.get("YANDEX_WEBMASTER_TOKEN", "").strip()
    if not t:
        raise RuntimeError(
            "YANDEX_WEBMASTER_TOKEN не задан. "
            "См. docs/seo/agent-system/access-checklist.md, пункт 3."
        )
    return t


def _headers() -> dict:
    return {"Authorization": f"OAuth {_token()}"}


def _get(path: str, params: Optional[dict] = None) -> dict:
    response = requests.get(f"{API_BASE}{path}", headers=_headers(), params=params or {}, timeout=20)
    response.raise_for_status()
    return response.json()


def _post(path: str, payload: Optional[dict] = None) -> dict:
    response = requests.post(
        f"{API_BASE}{path}",
        headers={**_headers(), "Content-Type": "application/json"},
        json=payload or {},
        timeout=20,
    )
    response.raise_for_status()
    if response.text:
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}
    return {}


def yw_get_user_id() -> int:
    """Вернуть user_id текущего владельца токена (нужен для всех остальных эндпоинтов)."""
    data = _get("/user/")
    return data["user_id"]


def yw_list_hosts(user_id: int) -> list[dict]:
    """Список сайтов, добавленных в Я.Вебмастер."""
    data = _get(f"/user/{user_id}/hosts/")
    return data.get("hosts", [])


def yw_host_info(user_id: int, host_id: str) -> dict:
    """Общая информация по сайту (ИКС, статус верификации и т.д.)."""
    return _get(f"/user/{user_id}/hosts/{quote(host_id, safe='')}/")


def yw_search_queries(
    user_id: int,
    host_id: str,
    date_from: str,
    date_to: str,
    query_indicator: str = "TOTAL_SHOWS",
    order_by: str = "TOTAL_SHOWS",
    limit: int = 25,
) -> list[dict]:
    """Поисковые запросы, по которым показывался сайт."""
    params = {
        "date_from": date_from,
        "date_to": date_to,
        "query_indicator": query_indicator,
        "order_by": order_by,
        "limit": limit,
    }
    data = _get(
        f"/user/{user_id}/hosts/{quote(host_id, safe='')}/search-queries/popular/",
        params=params,
    )
    return data.get("queries", [])


def yw_recrawl_quota(user_id: int, host_id: str) -> dict:
    """Возвращает квоту recrawl: { quota_remaining_daily, daily_quota }.

    Docs: https://yandex.ru/dev/webmaster/doc/dg/reference/host-recrawl-get.html
    """
    return _get(f"/user/{user_id}/hosts/{quote(host_id, safe='')}/recrawl/quota/")


def yw_recrawl_url(user_id: int, host_id: str, url: str) -> dict:
    """Поставить URL на переобход. Возвращает {"task_id": "..."} при успехе.

    Возможные HTTP-ошибки:
      400 — невалидный URL или URL не принадлежит хосту
      403 — квота исчерпана
      404 — host_id не найден

    Docs: https://yandex.ru/dev/webmaster/doc/dg/reference/host-recrawl-post.html
    """
    payload = {"url": url}
    return _post(f"/user/{user_id}/hosts/{quote(host_id, safe='')}/recrawl/queue/", payload)


def yw_resolve_host_id(user_id: Optional[int] = None, host_url: str = "example.com") -> tuple[int, str]:
    """Найти host_id по части URL. Возвращает (user_id, host_id).

    Кэширует ничего — каждый раз ходит в API. Для частых вызовов лучше прокинуть
    YANDEX_WEBMASTER_USER_ID и YANDEX_WEBMASTER_HOST_ID через env.
    """
    env_uid = os.environ.get("YANDEX_WEBMASTER_USER_ID", "").strip()
    env_hid = os.environ.get("YANDEX_WEBMASTER_HOST_ID", "").strip()
    if env_uid and env_hid:
        return int(env_uid), env_hid

    if user_id is None:
        user_id = yw_get_user_id()
    hosts = yw_list_hosts(user_id)

    matches = [
        h for h in hosts
        if host_url in h.get("unicode_host_url", "") or host_url in h.get("ascii_host_url", "")
    ]
    if not matches:
        raise RuntimeError(f"Хост '{host_url}' не найден среди {len(hosts)} hosts. "
                           f"Проверь, что сайт добавлен и верифицирован в Я.Вебмастере.")

    # Предпочитаем https-зеркало и верифицированный хост — иначе recrawl на
    # http-зеркало вернёт 400 (несоответствие схемы каноническому URL).
    def rank(h: dict) -> tuple:
        url = h.get("unicode_host_url", "")
        return (
            0 if h.get("verified") else 1,
            0 if url.startswith("https://") else 1,
        )

    matches.sort(key=rank)
    return user_id, matches[0]["host_id"]


def yw_sqi_history(user_id: int, host_id: str, date_from: str, date_to: str) -> list[dict]:
    """История ИКС (индекса качества сайта) по датам."""
    params = {"date_from": date_from, "date_to": date_to}
    data = _get(
        f"/user/{user_id}/hosts/{quote(host_id, safe='')}/sqi-history/",
        params=params,
    )
    return data.get("points", [])


if __name__ == "__main__":
    # Smoke-test.
    import datetime as dt
    logging.basicConfig(level=logging.INFO)

    print("→ Получаю user_id...")
    user_id = yw_get_user_id()
    print(f"  user_id = {user_id}")

    print("\n=== Сайты в Я.Вебмастере ===")
    hosts = yw_list_hosts(user_id)
    if not hosts:
        print("  ⚠ Список пуст. Проверь, что токен выдан для нужного аккаунта.")
        raise SystemExit(0)

    ped_host_id = None
    for h in hosts:
        verified = "✓" if h.get("verified") else "✗"
        print(f"  {verified} {h['unicode_host_url']}  (host_id={h['host_id']})")
        if "example.com" in h["unicode_host_url"]:
            ped_host_id = h["host_id"]

    if not ped_host_id:
        print("\n⚠ example.com не найден в списке. Подключи сайт в Я.Вебмастере.")
        raise SystemExit(0)

    print(f"\n=== Сводка для example.com (host_id={ped_host_id}) ===")
    info = yw_host_info(user_id, ped_host_id)
    print(f"  Верификация: {info.get('verified')}")
    print(f"  Главное зеркало: {info.get('main_mirror_host_id') or '—'}")
    print(f"  ИКС: {info.get('sqi') if 'sqi' in info else '—'}")

    # Запросы за прошлую неделю (data lag в Я.Вебмастере ~3 дня, поэтому даты —8..—3)
    date_to = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    date_from = (dt.date.today() - dt.timedelta(days=10)).isoformat()
    print(f"\n=== Top-10 запросов с показами ({date_from} → {date_to}) ===")
    try:
        queries = yw_search_queries(user_id, ped_host_id, date_from, date_to, limit=10)
        if not queries:
            print("  (нет данных за период — обычно для нового домена под Disallow: /)")
        else:
            for q in queries:
                shows = q.get("indicators", {}).get("TOTAL_SHOWS", 0)
                clicks = q.get("indicators", {}).get("TOTAL_CLICKS", 0)
                print(f"  {clicks:>4} кликов · {shows:>6} показов · «{q.get('query_text')}»")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            print("  (нет данных за период — API вернул 404, нормально для свежей property)")
        else:
            print(f"  ⚠ Ошибка: {e}")
