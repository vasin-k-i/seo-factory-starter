#!/usr/bin/env python3
"""Живая выдача Яндекса и Google через XMLRiver: позиции, топ конкурентов, кластеризация.

Зачем. Три задачи, за которые мы платили разным сервисам или не делали вовсе:

1. **Съём позиций.** TopVisor ~0,09 ₽ за проверку — здесь 0,025 ₽, в 3,5 раза дешевле.
2. **Кластеризация семантики по выдаче.** Арсенкин берёт 2 юнита за фразу (~0,05 ₽);
   здесь топ-10 стоит 0,025 ₽, а группировку считаем сами — вдвое дешевле, и, что важнее,
   **сами URL топа остаются у нас**: их можно переиспользовать для анализа конкурентов
   и реверса структуры, не платя второй раз.
3. **Кто реально в топе по нашим темам** — сырьё для «объёмнее любого конкурента».

Кластеризация — soft по пересечению URL: два запроса в одной группе, если их топы
делят >= `threshold` общих адресов. Это тот же принцип, что у платных сервисов;
порог 3 при глубине 10 — рабочий компромисс (4–5 = жёстче, дробит на мелкие группы).

CLI:
    python3 modules/serp.py top "колледж дистанционно" --depth 30
    python3 modules/serp.py pos ped-college.ru --in queries.txt
    python3 modules/serp.py cluster --in queries.txt --out clusters.json

Ключи: XMLRIVER_USER + XMLRIVER_KEY. Бюджет и лимит на проект — общие с freq.py
(`SEO_DAILY_RUB_LIMIT`, журнал `~/.seo-freq/spend.json`).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from freq import (  # noqa: E402
    RUB_PER_XMLRIVER, _load_env, _record_spend, daily_rub_limit, normalize,
    project_name, rub_room,
)

import os  # noqa: E402

ENGINES = {
    "yandex": "http://xmlriver.com/search_yandex/xml",
    "google": "http://xmlriver.com/search/xml",
}
PER_PAGE = 10          # сколько выдача отдаёт за один запрос
MAX_RETRIES = 4


def _creds() -> tuple[str, str]:
    _load_env()
    return ((os.environ.get("XMLRIVER_USER") or "").strip(),
            (os.environ.get("XMLRIVER_KEY") or "").strip())


def _fetch(engine: str, query: str, region: int, page: int) -> list[str]:
    """Одна страница выдачи. Пустой список = не смогли получить (НЕ «пусто в выдаче»).

    ⚠️ Сервис штатно отвечает `<error code="500">Выполните перезапрос. Ответ от поисковой
    системы не получен` — это просьба повторить, а не отказ. Без ретрая запрос молча
    выпадает, и страница выглядит как «нас нет в топе».
    """
    user, key = _creds()
    if not user or not key:
        return []
    base = ENGINES[engine]
    q = urllib.parse.quote(query)
    geo = f"&lr={region}" if engine == "yandex" else f"&loc={region}"
    url = f"{base}?user={user}&key={key}&query={q}&groupby={PER_PAGE}{geo}&page={page}"

    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                body = r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            time.sleep(2 * (attempt + 1))
            if attempt == MAX_RETRIES - 1:
                print(f"  [serp] сеть на «{query}» стр.{page}: {e}")
            continue
        if "закончились деньги" in body:
            print("  [serp] на счету XMLRiver закончились деньги — стоп")
            return []
        err = re.search(r'<error code="(\d+)">(.*?)</error>', body)
        if err:
            time.sleep(3 * (attempt + 1))
            continue
        _record_spend(xr=1, rub=RUB_PER_XMLRIVER)
        return re.findall(r"<url>(.*?)</url>", body)
    print(f"  [serp] «{query}» стр.{page}: не получена после {MAX_RETRIES} попыток")
    return []


def top(query: str, *, engine: str = "yandex", region: int = 225,
        depth: int = 10) -> list[str]:
    """Топ выдачи по запросу. depth округляется вверх до целых страниц по 10."""
    pages = max(1, (depth + PER_PAGE - 1) // PER_PAGE)
    need = pages * RUB_PER_XMLRIVER
    if rub_room() < need:
        print(f"  [serp] дневной лимит {daily_rub_limit()} ₽ на «{project_name()}» "
              f"не даёт снять {pages} стр. — пропуск «{query}»")
        return []
    urls: list[str] = []
    for p in range(pages):
        urls += _fetch(engine, query, region, p)
    # дубли между страницами бывают — выдача живая и может сдвинуться между вызовами
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:depth]


def _host(url: str) -> str:
    try:
        h = urllib.parse.urlparse(url).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except ValueError:
        return ""


def positions(domain: str, queries: list[str], *, engine: str = "yandex",
              region: int = 225, depth: int = 30) -> dict[str, int | None]:
    """{запрос: позиция или None}. None = не найден в пределах depth ИЛИ не снялось.

    Разницу между «нас нет в топе» и «выдача не пришла» видно по логу выше: если
    страницу не удалось получить, там будет строка [serp]. Молча ноль не ставим.
    """
    domain = domain.lower().removeprefix("www.")
    out: dict[str, int | None] = {}
    for q in queries:
        urls = top(q, engine=engine, region=region, depth=depth)
        pos = None
        for i, u in enumerate(urls, 1):
            if _host(u) == domain or _host(u).endswith("." + domain):
                pos = i
                break
        out[q] = pos
    return out


def cluster(queries: list[str], *, engine: str = "yandex", region: int = 225,
            depth: int = 10, threshold: int = 3) -> list[dict]:
    """Soft-кластеризация по пересечению топов.

    Жадно: самый «связный» запрос становится ядром группы, к нему цепляются все,
    у кого с ядром >= threshold общих URL. Запрос, чей топ не снялся, в кластеры
    не попадает — иначе он склеился бы со всеми подряд по пустому пересечению.
    """
    tops: dict[str, set[str]] = {}
    for q in queries:
        urls = top(q, engine=engine, region=region, depth=depth)
        if urls:
            tops[q] = {_host(u) + urllib.parse.urlparse(u).path.rstrip("/") for u in urls}

    unassigned = dict(tops)
    clusters: list[dict] = []
    while unassigned:
        # ядро — запрос с наибольшим числом «соседей»
        core = max(unassigned,
                   key=lambda a: sum(1 for b in unassigned
                                     if a != b and len(unassigned[a] & unassigned[b]) >= threshold))
        members = [core] + [b for b in unassigned
                            if b != core and len(unassigned[core] & unassigned[b]) >= threshold]
        common = set.intersection(*(unassigned[m] for m in members)) if members else set()
        clusters.append({
            "core": core,
            "queries": members,
            "size": len(members),
            "common_urls": sorted(common)[:10],
        })
        for m in members:
            unassigned.pop(m, None)

    skipped = [q for q in queries if q not in tops]
    if skipped:
        print(f"  [serp] не снялись и не кластеризованы: {len(skipped)} "
              f"(это НЕ «нет конкурентов», а несостоявшийся запрос)")
    clusters.sort(key=lambda c: -c["size"])
    return clusters


def _read_queries(args) -> list[str]:
    qs = list(args.queries or [])
    if args.infile:
        qs += [l.strip() for l in Path(args.infile).read_text(encoding="utf-8").splitlines()
               if l.strip()]
    return [normalize(q) for q in qs if q.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Выдача Яндекса/Google через XMLRiver")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--engine", choices=["yandex", "google"], default="yandex")
    common.add_argument("--region", type=int, default=225)
    common.add_argument("--in", dest="infile")
    common.add_argument("--out")

    p_top = sub.add_parser("top", parents=[common], help="топ выдачи по запросу")
    p_top.add_argument("queries", nargs="*")
    p_top.add_argument("--depth", type=int, default=10)

    p_pos = sub.add_parser("pos", parents=[common], help="позиции домена по запросам")
    p_pos.add_argument("domain")
    p_pos.add_argument("queries", nargs="*")
    p_pos.add_argument("--depth", type=int, default=30)

    p_cl = sub.add_parser("cluster", parents=[common], help="кластеризация по выдаче")
    p_cl.add_argument("queries", nargs="*")
    p_cl.add_argument("--depth", type=int, default=10)
    p_cl.add_argument("--threshold", type=int, default=3)

    args = ap.parse_args()
    qs = _read_queries(args)
    if not qs:
        ap.error("нет запросов: дай аргументами или --in файл")

    if args.cmd == "top":
        result = {}
        for q in qs:
            urls = top(q, engine=args.engine, region=args.region, depth=args.depth)
            result[q] = urls
            print(f"\n«{q}» — {len(urls)} позиций")
            for i, u in enumerate(urls, 1):
                print(f"  {i:>3}. {u[:96]}")
    elif args.cmd == "pos":
        result = positions(args.domain, qs, engine=args.engine,
                           region=args.region, depth=args.depth)
        print(f"\nпозиции {args.domain} (глубина {args.depth}):")
        for q, p in sorted(result.items(), key=lambda kv: (kv[1] is None, kv[1] or 0)):
            print(f"  {str(p) if p else '—':>4}  {q}")
    else:
        result = cluster(qs, engine=args.engine, region=args.region,
                         depth=args.depth, threshold=args.threshold)
        print(f"\n{len(result)} кластеров из {len(qs)} запросов "
              f"(порог {args.threshold} общих URL):")
        for c in result:
            print(f"\n  ▸ {c['core']}  ({c['size']} запр.)")
            for q in c["queries"][1:]:
                print(f"      {q}")

    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"\n→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
