"""
HTML-проверки для M2 (tech audit).

Каждая check-функция получает (url, soup) и возвращает list[Issue].
Глобальные проверки (дубли title/desc) — отдельно, после прохода по всем страницам.

Severity:
    critical — индексация под угрозой (нет canonical, noindex по ошибке, http 5xx)
    high     — заметный SEO-урон (нет title/h1, http 4xx, broken JSON-LD)
    medium   — мелкие огрехи (длина title >60, пустой alt)
    low      — косметика (дубль H2, отсутствие meta description)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Iterable
from urllib.parse import urlparse

from bs4 import BeautifulSoup


@dataclass
class Issue:
    url: str
    check: str
    severity: str
    detail: str

    def key(self) -> tuple[str, str, str]:
        """Уникальный ключ для дедупликации/дельты между аудитами."""
        return (self.url, self.check, self.detail)

    def to_dict(self) -> dict:
        return asdict(self)


# ───── Атомарные проверки ────────────────────────────────────────────

def check_title(url: str, soup: BeautifulSoup) -> list[Issue]:
    title_tag = soup.find("title")
    if not title_tag or not (title_tag.text or "").strip():
        return [Issue(url, "title_missing", "high", "нет <title> или он пустой")]
    title = title_tag.text.strip()
    issues: list[Issue] = []
    if len(title) > 60:
        issues.append(Issue(url, "title_too_long", "medium", f"title={len(title)} символов, рекомендуется ≤60"))
    if len(title) < 20:
        issues.append(Issue(url, "title_too_short", "low", f"title={len(title)} символов, рекомендуется ≥20"))
    return issues


def check_description(url: str, soup: BeautifulSoup) -> list[Issue]:
    desc = soup.find("meta", attrs={"name": "description"})
    if not desc or not (desc.get("content") or "").strip():
        return [Issue(url, "description_missing", "high", "нет meta description или он пустой")]
    content = desc["content"].strip()
    issues: list[Issue] = []
    if len(content) > 160:
        issues.append(Issue(url, "description_too_long", "medium", f"desc={len(content)} символов, рекомендуется ≤160"))
    if len(content) < 70:
        issues.append(Issue(url, "description_too_short", "low", f"desc={len(content)} символов, рекомендуется ≥70"))
    return issues


def check_canonical(url: str, soup: BeautifulSoup) -> list[Issue]:
    link = soup.find("link", rel="canonical")
    if not link or not (link.get("href") or "").strip():
        return [Issue(url, "canonical_missing", "high", "нет <link rel=canonical>")]
    canonical = link["href"].strip()

    # Нормализуем для сравнения: убираем trailing slash (кроме /), сравниваем по path
    def norm(u: str) -> str:
        p = urlparse(u)
        path = p.path.rstrip("/") or "/"
        return f"{p.scheme}://{p.netloc}{path}"

    if norm(canonical) != norm(url):
        return [Issue(url, "canonical_mismatch", "critical",
                      f"canonical={canonical} ≠ url={url}")]
    return []


def check_h1(url: str, soup: BeautifulSoup) -> list[Issue]:
    h1s = soup.find_all("h1")
    if not h1s:
        return [Issue(url, "h1_missing", "high", "нет <h1>")]
    if len(h1s) > 1:
        return [Issue(url, "h1_multiple", "medium",
                      f"найдено {len(h1s)} <h1>, должен быть 1")]
    text = (h1s[0].text or "").strip()
    if not text:
        return [Issue(url, "h1_empty", "high", "<h1> пустой")]
    if len(text) > 100:
        return [Issue(url, "h1_too_long", "low", f"h1={len(text)} символов, рекомендуется ≤100")]
    return []


def check_meta_robots(url: str, soup: BeautifulSoup) -> list[Issue]:
    """Ловим noindex/nofollow на страницах, которые должны индексироваться.

    Q-012 / Q-014: страницы /policy, /soglasie, /offer, /litsenziya — намеренно noindex.
    Их пропускаем (severity=info, не пишем).
    """
    NOINDEX_PATHS_OK = {"/policy", "/soglasie", "/offer", "/litsenziya", "/thanks"}
    parsed = urlparse(url)
    if parsed.path.rstrip("/") in {p.rstrip("/") for p in NOINDEX_PATHS_OK}:
        return []  # ожидаемо noindex, не алертим

    robots = soup.find("meta", attrs={"name": "robots"})
    if robots:
        content = (robots.get("content") or "").lower()
        if "noindex" in content:
            return [Issue(url, "noindex_on_public_page", "critical",
                          f"meta robots={content!r} на странице, которая должна индексироваться")]
    return []


def check_alt(url: str, soup: BeautifulSoup) -> list[Issue]:
    """alt="" — валидно (декоративное по WCAG), алертим только если атрибут отсутствует."""
    imgs = soup.find_all("img")
    missing = [
        img for img in imgs
        if img.get("alt") is None
        and img.get("aria-hidden") != "true"
        and img.get("role") != "presentation"
    ]
    if missing:
        return [Issue(url, "img_no_alt", "medium",
                      f"{len(missing)} из {len(imgs)} <img> без атрибута alt")]
    return []


def check_json_ld(url: str, soup: BeautifulSoup) -> list[Issue]:
    scripts = soup.find_all("script", type="application/ld+json")
    if not scripts:
        return [Issue(url, "json_ld_missing", "medium", "нет JSON-LD на странице")]

    issues: list[Issue] = []
    for i, script in enumerate(scripts):
        raw = (script.string or script.text or "").strip()
        if not raw:
            issues.append(Issue(url, "json_ld_empty", "medium", f"JSON-LD #{i+1} пустой"))
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            issues.append(Issue(url, "json_ld_invalid", "high",
                                f"JSON-LD #{i+1}: {e.msg} (line {e.lineno})"))
            continue
        # Минимальная проверка @context / @type
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if "@type" not in item:
                issues.append(Issue(url, "json_ld_no_type", "low",
                                    f"JSON-LD #{i+1} без @type"))
            # Проверка на буквальные <<TBD>>/<<HIDDEN>> в JSON
            raw_str = json.dumps(item, ensure_ascii=False)
            if "<<TBD" in raw_str or "<<HIDDEN" in raw_str:
                issues.append(Issue(url, "json_ld_tbd_marker", "critical",
                                    f"JSON-LD #{i+1} содержит <<TBD>> или <<HIDDEN>> — нельзя в проде"))
    return issues


def check_redirects(url: str, final_url: str, redirects: list[str]) -> list[Issue]:
    if not redirects:
        return []
    if len(redirects) >= 3:
        chain = " → ".join(redirects + [final_url])
        return [Issue(url, "redirect_chain", "medium",
                      f"цепочка {len(redirects)} редиректов: {chain}")]
    return []


# ───── Прогон по странице ────────────────────────────────────────────

def run_page_checks(url: str, html: str) -> list[Issue]:
    soup = BeautifulSoup(html, "html.parser")
    issues: list[Issue] = []
    issues.extend(check_title(url, soup))
    issues.extend(check_description(url, soup))
    issues.extend(check_canonical(url, soup))
    issues.extend(check_h1(url, soup))
    issues.extend(check_meta_robots(url, soup))
    issues.extend(check_alt(url, soup))
    issues.extend(check_json_ld(url, soup))
    return issues


# ───── Глобальные проверки (после прохода по всем страницам) ─────────

def collect_titles_and_descs(crawl_results: Iterable) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Собрать словари title→[urls] и desc→[urls] для поиска дублей."""
    titles: dict[str, list[str]] = {}
    descs: dict[str, list[str]] = {}
    for cr in crawl_results:
        if not cr.html:
            continue
        soup = BeautifulSoup(cr.html, "html.parser")
        t = soup.find("title")
        if t and (t.text or "").strip():
            titles.setdefault(t.text.strip(), []).append(cr.url)
        d = soup.find("meta", attrs={"name": "description"})
        if d and (d.get("content") or "").strip():
            descs.setdefault(d["content"].strip(), []).append(cr.url)
    return titles, descs


def check_duplicates(crawl_results: Iterable) -> list[Issue]:
    """Глобальная проверка: одинаковый title или desc на ≥2 страницах = каннибализация."""
    titles, descs = collect_titles_and_descs(crawl_results)
    issues: list[Issue] = []
    for title, urls in titles.items():
        if len(urls) >= 2:
            for u in urls:
                others = [x for x in urls if x != u]
                issues.append(Issue(u, "duplicate_title", "high",
                                    f"тот же <title> ещё на {len(others)} стр: {others[0]}"
                                    + (f" и ещё {len(others)-1}" if len(others) > 1 else "")))
    for desc, urls in descs.items():
        if len(urls) >= 2:
            for u in urls:
                others = [x for x in urls if x != u]
                issues.append(Issue(u, "duplicate_description", "medium",
                                    f"тот же meta description ещё на {len(others)} стр: {others[0]}"
                                    + (f" и ещё {len(others)-1}" if len(others) > 1 else "")))
    return issues
