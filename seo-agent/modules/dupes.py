"""
Модуль `dupes` — четыре проверки, которых заводу не хватало.

Смысл: эти вещи видны из статики за секунды, но типовой SEO-конвейер их не ловит —
и их находит внешний аудитор раз в квартал за деньги. Пусть находит завод.

Портирован из боевого college-site 20.08.2026 после внешнего аудита, который
нашёл на живом сайте ровно это: коммерческие страницы, совпадающие между собой
на две трети текста, и статьи блога, отбирающие запросы у продающих страниц.

Что считает:

1. near-dupes коммерческих страниц — доля общих 5-словных сочетаний по зоне <main>.
   Шапка/подвал/меню исключены, иначе сквозные блоки дают ложные 40–50%.
   Мера — от МЕНЬШЕГО из двух текстов (контейнмент, не Жаккар): страница на 4 000
   символов, целиком повторённая внутри страницы на 20 000, должна давать 100%,
   а Жаккар покажет 20% и проблему спрячет.

2. пары «коммерческая страница ↔ статья блога» с близкими title (мера Жаккара по
   значимым словам) — кандидаты в каннибализацию: когда статья и посадка метят
   в один запрос, поисковик выбирает одну, и обычно не ту, где форма.

3. теговые листинги: сколько уходит в индекс при текущем пороге и что будет при
   других порогах. Тонкий листинг без своего текста — мусор в индексе.

4. покрытие sitemap: адреса из внутренних ссылок, которых нет в карте.
   ⚠️ Осознанные исключения (noindex-страницы, кластерные дубли с чужим
   canonicalUrl) в отчёт не идут — иначе модуль будет каждый раз предлагать
   «починить» то, что сделано специально.

Запуск: `python3 seo-agent/orchestrator.py dupes [--json]`

Настройка под свой сайт (переменные окружения):
    SITE_URL                 адрес сайта
    DUPES_GROUPS             JSON: группы однотипных страниц для сравнения
    DUPES_CATALOG_PATH       страница-каталог, откуда набрать карточки
    DUPES_CATALOG_ITEM_RE    regex пути карточки, напр. /katalog/[a-z0-9-]+
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

THIS_DIR = Path(__file__).resolve().parent.parent
REPO = THIS_DIR.parent
DATA_DIR = THIS_DIR / "data" / "dupes"

# Порог, с которого пара считается проблемной. 65% — то, что аудит нашёл у нас
# на страницах направлений; контроль на двух случайных статьях блога дал 7%,
# то есть метод различает уникальные тексты и не завышает.
NEAR_DUPE_WARN = 0.45
TITLE_PAIR_WARN = 0.45
NGRAM = 5

STOP_WORDS = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то", "все",
    "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за", "бы", "по",
    "только", "ее", "мне", "было", "вот", "от", "меня", "еще", "нет", "о", "из", "ему",
    "теперь", "когда", "даже", "ну", "вдруг", "ли", "если", "уже", "или", "ни", "быть",
    "был", "него", "до", "вас", "нибудь", "опять", "уж", "вам", "ведь", "там", "потом",
    "себя", "ничего", "ей", "может", "они", "тут", "где", "есть", "надо", "ней", "для",
    "мы", "тебя", "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз",
    "тоже", "себе", "под", "будет", "ж", "тогда", "кто", "этот", "того", "потому",
    "этого", "какой", "совсем", "ним", "здесь", "этом", "один", "почти", "мой", "тем",
    "чтобы", "нее", "сейчас", "были", "куда", "зачем", "всех", "никогда", "можно",
    "при", "наконец", "два", "об", "другой", "хоть", "после", "над", "больше", "тот",
    "через", "эти", "нас", "про", "всего", "них", "какая", "много", "разве", "три",
    "эту", "моя", "впрочем", "хорошо", "свою", "этой", "перед", "иногда", "лучше",
    "чуть", "том", "нельзя", "такой", "им", "более", "всегда", "конечно", "всю", "между",
}


def _words(text: str) -> list[str]:
    return re.findall(r"[а-яёa-z0-9]+", text.lower())


def _shingles(text: str, n: int = NGRAM) -> set[str]:
    w = _words(text)
    if len(w) < n:
        return set()
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def _containment(a: set[str], b: set[str]) -> float:
    """Доля общего от МЕНЬШЕГО множества. Ноль, если любое из них пустое."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _jaccard_words(a: str, b: str) -> float:
    wa = {w for w in _words(a) if w not in STOP_WORDS and len(w) > 2}
    wb = {w for w in _words(b) if w not in STOP_WORDS and len(w) > 2}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"&[a-z]+;|&#\d+;", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def _main_zone(html: str) -> str:
    """Только содержательная зона. Без этого сквозные шапка/подвал дают ложные проценты."""
    m = re.search(r"(?is)<main[^>]*>(.*?)</main>", html)
    if m:
        return _strip_html(m.group(1))
    # у части шаблонов <main> нет — тогда режем шапку и подвал вручную
    body = re.sub(r"(?is)<(header|footer|nav)[^>]*>.*?</\1>", " ", html)
    return _strip_html(body)


# ---------------------------------------------------------------- источники данных

def _fetch(url: str, timeout: int = 30) -> str | None:
    """
    ⚠️ urllib НЕ следует за 308 (Permanent Redirect) — а Next отдаёт именно 308
    на правила из next.config.mjs. Из-за этого первая версия помечала все
    склеенные старые слаги как «не отвечает» и выдавала 27 несуществующих
    «пропусков карты». Добавлен обработчик: 308 обрабатывается как 301.
    """
    import urllib.request

    class _Follow308(urllib.request.HTTPRedirectHandler):
        def http_error_308(self, req, fp, code, msg, headers):
            return self.http_error_301(req, fp, 301, msg, headers)

    opener = urllib.request.build_opener(_Follow308)
    req = urllib.request.Request(url, headers={"User-Agent": "seo-agent/dupes"})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def _final_path(url: str, timeout: int = 20) -> str | None:
    """Куда адрес приезжает после редиректов. None — если не отвечает вовсе."""
    import urllib.request

    class _Follow308(urllib.request.HTTPRedirectHandler):
        def http_error_308(self, req, fp, code, msg, headers):
            return self.http_error_301(req, fp, 301, msg, headers)

    opener = urllib.request.build_opener(_Follow308)
    req = urllib.request.Request(url, headers={"User-Agent": "seo-agent/dupes"})
    try:
        with opener.open(req, timeout=timeout) as r:
            return _to_path(r.geturl())
    except Exception:
        return None


def _fetch_many(urls: list[str], workers: int = 12) -> dict[str, str]:
    """Параллельная выкачка: на корпусе в ~1000 страниц последовательная занимает
    минуты и модуль перестают запускать. 12 потоков — щадящий режим для своего же
    сервера, не DDoS."""
    from concurrent.futures import ThreadPoolExecutor
    out: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for url, html in zip(urls, ex.map(_fetch, urls)):
            if html:
                out[url] = html
    return out


def _to_path(url: str) -> str:
    """
    Всё внутри модуля живём в ПУТЯХ, а не в абсолютных адресах.
    ⚠️ Первая версия сравнивала абсолютные url — и молча ломалась, когда база
    запуска и база в sitemap.xml разные (прогон против localhost при
    NEXT_PUBLIC_SITE_URL=https://ped-college.ru). Пересечений не находилось,
    проверка честно печатала «0 пар» и выглядела зелёной.
    """
    from urllib.parse import urlparse
    pa = urlparse(url).path or "/"
    return pa if pa == "/" else pa.rstrip("/")


#: то, что вообще не является страницей — бандлы, картинки, шрифты, документы.
#: Без этого фильтра «пропуски карты» захлёбываются ассетами (/_next/…, /images/…,
#: /favicon.ico) и список на 700 строк никто не читает. Проверка, которая кричит
#: волками, перестаёт работать вовсе.
_ASSET_RE = re.compile(
    r"(^/_next/)|(^/api/)|(\.(?:js|css|woff2?|ttf|png|jpe?g|webp|avif|svg|gif|ico|pdf|xml|txt|json|mp4|webm)$)",
    re.I,
)


def _is_page(path: str) -> bool:
    return bool(path.startswith("/")) and not _ASSET_RE.search(path) and " " not in path


def _site_base() -> str:
    return (os.environ.get("SITE_URL") or os.environ.get("M2_SITE_ROOT")
            or os.environ.get("NEXT_PUBLIC_SITE_URL") or "https://example.com")


def _sitemap_urls(base: str) -> list[str]:
    out: list[str] = []
    queue = [f"{base}/sitemap.xml"]
    seen: set[str] = set()
    while queue:
        sm = queue.pop()
        if sm in seen:
            continue
        seen.add(sm)
        x = _fetch(sm)
        if not x:
            continue
        subs = re.findall(r"<sitemap>.*?<loc>(.*?)</loc>", x, re.S)
        if subs:
            queue += subs
        else:
            out += re.findall(r"<url>.*?<loc>(.*?)</loc>", x, re.S)
    return sorted(set(out))


# ---------------------------------------------------------------- проверки

@dataclass
class Report:
    near_dupes: list[dict] = field(default_factory=list)
    title_pairs: list[dict] = field(default_factory=list)
    duplicate_titles: list[dict] = field(default_factory=list)
    tags: dict = field(default_factory=dict)
    sitemap: dict = field(default_factory=dict)


def check_near_dupes(base: str, groups: dict[str, list[str]]) -> list[dict]:
    """Попарное сравнение внутри каждой группы однотипных страниц."""
    found: list[dict] = []
    for group, paths in groups.items():
        pages = _fetch_many([base + p for p in paths])
        texts: dict[str, str] = {p: _main_zone(pages[base + p]) for p in paths if base + p in pages}
        shing = {p: _shingles(t) for p, t in texts.items()}
        keys = sorted(shing)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                score = _containment(shing[a], shing[b])
                if score >= NEAR_DUPE_WARN:
                    found.append({
                        "group": group, "a": a, "b": b,
                        "overlap": round(score, 3),
                        "chars_a": len(texts[a]), "chars_b": len(texts[b]),
                    })
    found.sort(key=lambda x: -x["overlap"])
    return found


def check_title_pairs(pages: dict[str, str], commercial: Iterable[str]) -> list[dict]:
    """Пары «коммерческая страница ↔ статья» с близкими title."""
    # ⚠️ ключи pages — ПОЛНЫЕ url, а не пути. Первая версия фильтровала по
    # startswith("/blog/") и всегда получала пустой список статей: проверка молча
    # возвращала 0 пар. Поймано сверкой с внешним аудитом, который на том же
    # корпусе нашёл 26 пар — если бы не с чем было сверить, «0 находок» выглядел
    # бы как «всё хорошо».
    comm = [p for p in commercial if p in pages]
    arts = [p for p in pages
            if "/blog/" in p and not any(x in p for x in ("/tag/", "/page/", "/category/", "/author/"))]
    out: list[dict] = []
    for c in comm:
        for a in arts:
            s = _jaccard_words(pages[c], pages[a])
            if s >= TITLE_PAIR_WARN:
                out.append({"score": round(s, 2), "commercial": c, "article": a,
                            "title_commercial": pages[c], "title_article": pages[a]})
    out.sort(key=lambda x: -x["score"])
    return out


def check_duplicate_titles(pages: dict[str, str]) -> list[dict]:
    by_title: dict[str, list[str]] = {}
    for url, title in pages.items():
        if title:
            by_title.setdefault(title.strip(), []).append(url)
    return [{"title": t, "urls": sorted(u)} for t, u in by_title.items() if len(u) > 1]


def check_tags() -> dict:
    """
    Распределение тегов по числу материалов и что даст каждый порог.

    ⚠️ Если корпуса статей нет (шаблон репозитория, другой формат контента) —
    возвращаем data_gap, а НЕ нули. Ноль тегов и «не смогли посчитать теги» —
    разные вещи, и подавать их одинаково нельзя: отчёт с нулями выглядит зелёным.
    """
    blog_dir = REPO / "content" / "blog"
    if not blog_dir.is_dir():
        return {"data_gap": f"нет каталога {blog_dir.relative_to(REPO)} — "
                            f"корпус статей не найден, теги не считались"}
    counts: Counter[str] = Counter()
    for p in blog_dir.glob("*.mdx"):
        try:
            fm = p.read_text(encoding="utf-8").split("---", 2)[1]
        except Exception:
            continue
        m = re.search(r"^tags:\n((?:\s+- .*\n)+)", fm, re.M)
        if m:
            for t in re.findall(r'-\s*"?([^"\n]+)"?', m.group(1)):
                counts[t.strip()] += 1
    dist = Counter(counts.values())
    thresholds = {str(t): sum(v for k, v in dist.items() if k >= t) for t in (2, 3, 4, 5, 6)}
    return {
        "total_tags": len(counts),
        "distribution": {str(k): v for k, v in sorted(dist.items())},
        "indexable_at_threshold": thresholds,
    }


def check_sitemap_coverage(base: str, sitemap: set[str], crawled_links: set[str]) -> dict:
    """
    Адреса, на которые сайт ссылается, но в карте не объявляет.
    ⚠️ Осознанные исключения отсеиваем — иначе модуль каждый раз будет требовать
    «починить» noindex-страницы и кластерные дубли, которые убраны специально.
    """
    missing = sorted(crawled_links - sitemap)
    fetched = _fetch_many([base + m for m in missing])
    intentional, real = [], []
    for url in missing:
        html = fetched.get(base + url)
        if html is None:
            real.append({"url": url, "reason": "не отвечает"})
            continue
        final = _final_path(base + url)
        if final and final != url:
            intentional.append({"url": url, "reason": f"редирект → {final}"})
            continue
        robots = re.search(r'(?is)<meta[^>]+name="robots"[^>]+content="([^"]*)"', html)
        canon = re.search(r'(?is)<link[^>]+rel="canonical"[^>]+href="([^"]*)"', html)
        if robots and "noindex" in robots.group(1).lower():
            intentional.append({"url": url, "reason": "noindex"})
        elif canon and _to_path(canon.group(1)) != url:
            intentional.append({"url": url, "reason": f"canonical → {canon.group(1)}"})
        else:
            real.append({"url": url, "reason": "индексируется, но карты нет"})
    return {"in_sitemap": len(sitemap), "linked": len(crawled_links),
            "intentional_exclusions": len(intentional), "gaps": real}


# ---------------------------------------------------------------- точка входа

def _commercial_groups() -> dict[str, list[str]]:
    """
    Группы однотипных коммерческих страниц для попарного сравнения.
    Задаются под свой сайт — переменной окружения DUPES_GROUPS в виде JSON:

        DUPES_GROUPS='{"направления":["/uslugi/a","/uslugi/b"],"посадки":["/lp-1","/lp-2"]}'

    Пусто — модуль всё равно сравнит автообнаруженные страницы каталога
    (см. DUPES_CATALOG_PATH), но группы лучше задать: сравнивать имеет смысл
    ОДНОТИПНЫЕ страницы, а не всё со всем.
    """
    raw = os.environ.get("DUPES_GROUPS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {k: list(v) for k, v in data.items() if isinstance(v, list)}
    except Exception:
        print("  ⚠️ DUPES_GROUPS: не разобрал JSON, группы пропущены", flush=True)
        return {}


#: Страница-каталог, со ссылок которой набираются однотипные карточки.
#: У college-site это /spetsialnosti; у интернет-магазина — раздел категории.
CATALOG_PATH = os.environ.get("DUPES_CATALOG_PATH", "")
CATALOG_ITEM_RE = os.environ.get("DUPES_CATALOG_ITEM_RE", "")


def run(dry_run: bool = False, as_json: bool = False) -> dict:
    base = _site_base().rstrip("/")
    rep = Report()

    print(f"dupes · сайт {base}", flush=True)

    # 1. near-dupes коммерческих страниц
    groups = _commercial_groups()
    if CATALOG_PATH and CATALOG_ITEM_RE:
        cat_html = _fetch(base + CATALOG_PATH)
        if cat_html:
            items = sorted(set(re.findall(rf'href="({CATALOG_ITEM_RE})"', cat_html)))
            if items:
                groups["каталог"] = items
    if not groups:
        print("  ⚠️ группы не заданы (DUPES_GROUPS / DUPES_CATALOG_PATH) — "
              "сравнивать нечего, шаг near-dupes пропущен", flush=True)
    rep.near_dupes = check_near_dupes(base, groups)
    print(f"  near-dupes: пар выше {int(NEAR_DUPE_WARN*100)}% — {len(rep.near_dupes)}", flush=True)

    # 2–3. title'ы: пары каннибализации и полные дубли
    sitemap = _sitemap_urls(base)
    print(f"  качаю {len(sitemap)} страниц для title'ов…", flush=True)
    corpus = _fetch_many(sitemap)
    titles: dict[str, str] = {}
    for url, html in corpus.items():
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        if m:
            titles[_to_path(url)] = _strip_html(m.group(1))
    commercial = [p for g in groups.values() for p in g]
    rep.title_pairs = check_title_pairs(titles, commercial)
    rep.duplicate_titles = check_duplicate_titles(titles)
    print(f"  пары title (каннибализация): {len(rep.title_pairs)}", flush=True)
    print(f"  полные дубли title: {len(rep.duplicate_titles)}", flush=True)

    # 4. теги
    rep.tags = check_tags()
    if "data_gap" in rep.tags:
        print(f"  теги: data_gap — {rep.tags['data_gap']}", flush=True)
    else:
        print(f"  тегов всего {rep.tags['total_tags']}, "
              f"в индексе при разных порогах: {rep.tags['indexable_at_threshold']}", flush=True)

    # 5. покрытие карты
    links: set[str] = set()
    for html in corpus.values():   # корпус уже выкачан выше, второй проход не нужен
        for href in re.findall(r'href="(/[^"#?]*)"', html):
            path = _to_path(href)
            if _is_page(path):
                links.add(path)
    rep.sitemap = check_sitemap_coverage(base, {_to_path(u) for u in sitemap}, links)
    print(f"  карта: {rep.sitemap['in_sitemap']} адресов, "
          f"реальных пропусков {len(rep.sitemap['gaps'])} "
          f"(осознанных исключений {rep.sitemap['intentional_exclusions']})", flush=True)

    result = {
        "near_dupes": rep.near_dupes,
        "title_pairs": rep.title_pairs,
        "duplicate_titles": rep.duplicate_titles,
        "tags": rep.tags,
        "sitemap": rep.sitemap,
    }

    if not dry_run:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        (DATA_DIR / f"{stamp}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  сохранено: seo-agent/data/dupes/{stamp}.json", flush=True)

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    return result


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv, as_json="--json" in sys.argv)
