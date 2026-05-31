"""
Google Search Console client.

Использование:
    from modules.gsc_client import gsc_service, query_search_analytics

    svc = gsc_service()
    rows = query_search_analytics(svc, "sc-domain:example.com",
                                  start_date="2026-05-13", end_date="2026-05-19",
                                  dimensions=["query", "page"])

ENV:
    GSC_SERVICE_ACCOUNT_FILE — путь к JSON service-account (относительно корня репо).
        Альтернативно: GSC_SERVICE_ACCOUNT_JSON — содержимое JSON в одну строку
        (для GitHub Actions Secret).
    GSC_SITE_URL — `sc-domain:example.com` для Domain property
                   или `https://example.com/` для URL-prefix.
"""

from __future__ import annotations

import json
import os
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Полный scope webmasters (включает чтение). Должен совпадать со scope, с которым
# gsc_oauth_setup.py выпускает refresh_token, иначе refresh падает с invalid_scope.
SCOPES = ["https://www.googleapis.com/auth/webmasters"]


def _load_credentials():
    """Загрузить credentials. Порядок попыток:
       1) OAuth user creds (GSC_OAUTH_REFRESH_TOKEN + CLIENT_ID + CLIENT_SECRET) — приоритет;
       2) Service-account JSON-blob (GSC_SERVICE_ACCOUNT_JSON);
       3) Service-account JSON-файл (GSC_SERVICE_ACCOUNT_FILE).
    """
    refresh_token = os.environ.get("GSC_OAUTH_REFRESH_TOKEN", "").strip()
    client_id = os.environ.get("GSC_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GSC_OAUTH_CLIENT_SECRET", "").strip()
    if refresh_token and client_id and client_secret:
        from google.oauth2.credentials import Credentials
        return Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )

    from google.oauth2 import service_account
    json_blob = os.environ.get("GSC_SERVICE_ACCOUNT_JSON", "").strip()
    if json_blob:
        info = json.loads(json_blob)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    file_path = os.environ.get("GSC_SERVICE_ACCOUNT_FILE", "").strip()
    if not file_path:
        raise RuntimeError(
            "Нет ни OAuth-, ни service-account credentials. "
            "См. docs/seo/agent-system/access-checklist.md, пункт 2."
        )

    # Если путь относительный — считаем от корня seo-agent/
    p = Path(file_path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[1] / p
    if not p.exists():
        raise FileNotFoundError(f"GSC_SERVICE_ACCOUNT_FILE не найден: {p}")
    return service_account.Credentials.from_service_account_file(str(p), scopes=SCOPES)


def gsc_service():
    """Возвращает авторизованный клиент Search Console API."""
    from googleapiclient.discovery import build

    creds = _load_credentials()
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def list_sites(svc) -> list[dict]:
    """Список property, к которым у service-account есть доступ."""
    response = svc.sites().list().execute()
    return response.get("siteEntry", [])


def query_search_analytics(
    svc,
    site_url: str,
    start_date: str,
    end_date: str,
    dimensions: Optional[list[str]] = None,
    row_limit: int = 1000,
) -> list[dict]:
    """Возвращает строки searchanalytics.query."""
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions or ["query"],
        "rowLimit": row_limit,
    }
    response = svc.searchanalytics().query(siteUrl=site_url, body=body).execute()
    return response.get("rows", [])


if __name__ == "__main__":
    # Smoke-test: показывает список property + первые 5 запросов за вчера.
    import datetime as dt
    logging.basicConfig(level=logging.INFO)

    svc = gsc_service()

    print("\n=== Properties, к которым есть доступ ===")
    sites = list_sites(svc)
    if not sites:
        print("⚠ Service-account не имеет доступа ни к одной property.")
        print("  Добавь его в GSC: Settings → Users and permissions → Add user.")
        print("  Email service-account нужно взять из JSON-файла (поле client_email).")
    else:
        for s in sites:
            print(f"  {s['siteUrl']}  ({s['permissionLevel']})")

    site_url = os.environ.get("GSC_SITE_URL", "sc-domain:example.com")
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    week_ago = (dt.date.today() - dt.timedelta(days=7)).isoformat()

    print(f"\n=== Top-5 запросов для {site_url} (неделя назад → вчера) ===")
    try:
        rows = query_search_analytics(svc, site_url, week_ago, yesterday, ["query"], row_limit=5)
        if not rows:
            print("  (нет данных за период — обычная история для нового домена с noindex)")
        else:
            for row in rows:
                q = row["keys"][0]
                print(f"  {row['clicks']:>4} кликов · {row['impressions']:>6} показов · «{q}»")
    except Exception as e:
        print(f"⚠ Ошибка запроса: {e}")
