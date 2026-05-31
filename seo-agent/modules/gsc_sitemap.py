"""
Google Search Console — Sitemaps API.

Использование:
    from modules.gsc_sitemap import submit_sitemap, get_sitemap_status

    submit_sitemap("sc-domain:example.com", "https://example.com/sitemap.xml")

Требует scope https://www.googleapis.com/auth/webmasters (write).
OAuth refresh_token уже выдан с этим scope в gsc_oauth_setup.py.

Docs:
  https://developers.google.com/webmaster-tools/v1/sitemaps/submit
  https://developers.google.com/webmaster-tools/v1/sitemaps/get

ПРИМЕЧАНИЕ. Для обычных URL (статьи блога, страницы программ) Google официально
рекомендует пинговать sitemap через этот метод. Indexing API недоступен —
он поддерживает только JobPosting и BroadcastEvent. Для блог-статей submit_sitemap
+ внутренняя перелинковка + IndexNow для Яндекса/Bing — потолок того, что можно
автоматизировать без серых техник.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/webmasters"]


def _writable_service():
    """Возвращает GSC client с write-scope (sitemaps.submit/delete).

    Порядок:
      1) OAuth user credentials — refresh_token уже выпущен с write-scope в gsc_oauth_setup.py.
      2) Service-account (если был выдан webmasters write через домен-вайд).
    """
    refresh_token = os.environ.get("GSC_OAUTH_REFRESH_TOKEN", "").strip()
    client_id = os.environ.get("GSC_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GSC_OAUTH_CLIENT_SECRET", "").strip()

    if refresh_token and client_id and client_secret:
        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
    else:
        # Fallback на gsc_client._load_credentials() — но он возвращает readonly.
        # Если кто-то будет вызывать без OAuth — упадёт на permission denied от Google,
        # это нормально, потому что метод требует write.
        from modules.gsc_client import _load_credentials  # type: ignore
        creds = _load_credentials()

    from googleapiclient.discovery import build
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def submit_sitemap(site_url: str, feedpath: str, *, dry_run: bool = False) -> dict:
    """Отправить sitemap на (пере)обработку. Google перечитает feedpath из указанного URL.

    site_url: 'sc-domain:example.com' для Domain property или 'https://example.com/'.
    feedpath: полный URL sitemap'а (например 'https://example.com/sitemap.xml').

    Возвращает: { ok: bool, site_url, feedpath, error?: str }.
    """
    if dry_run:
        log.info("DRY-RUN submit_sitemap site=%s feed=%s", site_url, feedpath)
        return {"ok": True, "site_url": site_url, "feedpath": feedpath, "dry_run": True}

    try:
        svc = _writable_service()
        # submit() возвращает HTTP 204 No Content при успехе — execute() даёт None/dict.
        svc.sitemaps().submit(siteUrl=site_url, feedpath=feedpath).execute()
        log.info("GSC sitemap submitted: site=%s feed=%s", site_url, feedpath)
        return {"ok": True, "site_url": site_url, "feedpath": feedpath}
    except Exception as e:  # noqa: BLE001
        log.error("GSC sitemap submit failed: %s", e)
        return {"ok": False, "site_url": site_url, "feedpath": feedpath, "error": str(e)}


def get_sitemap_status(site_url: str, feedpath: str) -> dict:
    """Прочитать статус sitemap (lastSubmitted, lastDownloaded, isPending, warnings, errors)."""
    svc = _writable_service()
    return svc.sitemaps().get(siteUrl=site_url, feedpath=feedpath).execute()


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    site = os.environ.get("GSC_SITE_URL", "sc-domain:example.com")
    feed = sys.argv[1] if len(sys.argv) > 1 else "https://example.com/sitemap.xml"

    print(f"→ submit_sitemap site={site} feed={feed}")
    r = submit_sitemap(site, feed)
    print(f"  result: {r}")

    print(f"\n→ get_sitemap_status site={site} feed={feed}")
    try:
        st = get_sitemap_status(site, feed)
        for k in ("lastSubmitted", "lastDownloaded", "isPending", "isSitemapsIndex", "type"):
            print(f"  {k}: {st.get(k)}")
        if st.get("contents"):
            for c in st["contents"]:
                print(f"  contents: type={c.get('type')} submitted={c.get('submitted')} indexed={c.get('indexed')}")
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ Не удалось получить статус: {e}")
