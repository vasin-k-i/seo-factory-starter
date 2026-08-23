#!/usr/bin/env python3
"""Механический гейт: то, что можно проверить кодом, проверяем кодом.

Разделение труда с аудитором: сюда — всё детерминированное (числа, ссылки, заголовки,
разметка, стоп-слова, объём). Модели остаётся только смысл — то, что кодом не поймать.

Так дешевле и надёжнее: если рерайт потерял ссылку, это видно за миллисекунду и без
токенов, а не после разговора с моделью.
"""
from __future__ import annotations

import re

from . import formats

# Канцелярит и оценочные эпитеты из промптов фабрики (правила Ильяхова).
STOP_WORDS = [
    "является", "осуществля", "реализует", "обладает", "представляет собой",
    "в настоящее время", "в современных условиях", "в рамках", "в связи с тем",
    "следует отметить", "таким образом", "данный", "данная",
    "качественн", "эффективн", "уникальн", "инновацион", "современн",
    "индивидуальный подход", "команда профессионалов", "не секрет",
]
NGRAM = 6
VOLUME_TOLERANCE = 0.12          # ±12 % по словам

# Пороги «оторвался ли пересказ от оригинала».
# Секция ниже MIN_REWRITE_SECTION уходит на повтор; документ ниже MIN_REWRITE_DOC
# не публикуется вовсе — маркировка на месте, публиковать нечего.
# Замеры 23.08.2026: ped-college (гуманитарный текст) — 91,5 %; detalmet (плотная
# техника: ГОСТы, марки стали, режимы сварки) — 41,3 %. На технике термины держат
# формулировки, и переписать их «другими словами» нечем — это предел метода, а не
# плохой промпт. Порог отсекает такие статьи вместо того, чтобы делать вид, что вышло.
MIN_REWRITE_SECTION = 60
MIN_REWRITE_DOC = 70


def words(text: str) -> list[str]:
    return re.findall(r"[\wЀ-ӿ]+", text.lower())


def ngrams(text: str, n: int = NGRAM) -> set[tuple]:
    w = words(text)
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def numbers(text: str) -> set[str]:
    """Числа без тех, что порождает сама разметка нумерованных списков."""
    cleaned = re.sub(r"^\s*\d+\.\s", "", text, flags=re.M)
    return set(re.findall(r"\d+(?:[.,]\d+)?", cleaned))


def links(text: str) -> set[str]:
    md = set(re.findall(r"\]\(([^)]+)\)", text))
    html = set(re.findall(r'href="([^"]+)"', text))
    return md | html


def stop_hits(text: str) -> dict:
    low = text.lower()
    return {w: low.count(w) for w in STOP_WORDS if low.count(w)}


def rewritten_share(orig: str, new: str) -> float:
    """Доля уничтоженных 6-словных цепочек. Ради неё всё и затевалось."""
    o = ngrams(orig)
    return 100 * (1 - len(o & ngrams(new)) / len(o)) if o else 0.0


def check_section(orig: str, new: str, fmt: str) -> list[str]:
    """Претензии к ОДНОЙ секции. Пустой список — секция принята."""
    problems = []

    lost_num = numbers(orig) - numbers(new)
    if lost_num:
        problems.append(f"потеряны числа: {', '.join(sorted(lost_num))}")
    new_num = numbers(new) - numbers(orig)
    if new_num:
        problems.append(f"появились числа, которых не было: {', '.join(sorted(new_num))}")

    lost_links = links(orig) - links(new)
    if lost_links:
        problems.append(f"потеряны ссылки: {', '.join(sorted(lost_links))}")

    sig_o, sig_n = (formats.markup_signature(orig, fmt),
                    formats.markup_signature(new, fmt))
    diff = {k: (sig_o.get(k, 0), sig_n.get(k, 0))
            for k in set(sig_o) | set(sig_n) if sig_o.get(k, 0) != sig_n.get(k, 0)}
    if diff:
        problems.append("разметка разъехалась: "
                        + "; ".join(f"{k} было {a}, стало {b}" for k, (a, b) in diff.items()))

    o_words, n_words = len(words(orig)), len(words(new))
    if o_words and abs(n_words - o_words) / o_words > VOLUME_TOLERANCE:
        problems.append(f"объём уехал: было {o_words} слов, стало {n_words}")

    o_stop, n_stop = stop_hits(orig), stop_hits(new)
    brought = {k: v for k, v in n_stop.items() if v > o_stop.get(k, 0)}
    if brought:
        problems.append("притащил канцелярит: "
                        + ", ".join(f"«{k}» ×{v}" for k, v in brought.items()))

    if rewritten_share(orig, new) < MIN_REWRITE_SECTION:
        problems.append(f"переписано всего {rewritten_share(orig, new):.0f}% — "
                        f"маркировка, скорее всего, уцелела")
    return problems


def check_document(orig_doc: formats.Doc, new_doc: formats.Doc) -> dict:
    """Сводка по всей статье — для отчёта, не для гейта."""
    o_body = orig_doc.body
    n_body = new_doc.body
    heads_ok = ([s.head for s in orig_doc.sections]
                == [s.head for s in new_doc.sections])
    return {
        "переписано_%": round(rewritten_share(o_body, n_body), 1),
        "слов_было": len(words(o_body)),
        "слов_стало": len(words(n_body)),
        "заголовки_целы": heads_ok,
        "числа_потеряны": sorted(numbers(o_body) - numbers(n_body)),
        "числа_лишние": sorted(numbers(n_body) - numbers(o_body)),
        "ссылки_потеряны": sorted(links(o_body) - links(n_body)),
    }
