#!/usr/bin/env python3
"""Адаптеры форматов статей: MDX/MD (флот на Next+MDX) и HTML (detalmet).

Зачем отдельный слой. Рерайт обязан сохранить разметку и заголовки — они завязаны на
`toc[].id`, на FAQPage JSON-LD и на вёрстку сайта. Полагаться на то, что модель их не
тронет, нельзя: она тронет. Поэтому заголовки и фронтматтер вырезаются ДО вызова модели
и приклеиваются обратно ПОСЛЕ. Модель их просто не видит.

Единый интерфейс: `parse(text, fmt) -> Doc`, `doc.render()` собирает обратно.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

FORMATS = ("mdx", "md", "html")


@dataclass
class Section:
    head: str           # строка заголовка целиком («## Что такое…» / «<h2>Что такое…</h2>»)
    body: str           # тело секции без заголовка — только его отдают модели
    label: str = ""     # человекочитаемое имя для логов и ТЗ на правку

    def render(self, fmt: str) -> str:
        if not self.head:
            return self.body.strip()
        joiner = "\n\n" if fmt in ("mdx", "md") else "\n"
        return f"{self.head}{joiner}{self.body.strip()}"


@dataclass
class Doc:
    fmt: str
    front: str = ""                       # фронтматтер MDX; у HTML пусто
    sections: list[Section] = field(default_factory=list)

    def render(self) -> str:
        sep = "\n\n" if self.fmt in ("mdx", "md") else "\n\n"
        body = sep.join(s.render(self.fmt) for s in self.sections if s.render(self.fmt))
        return (self.front + "\n" + body if self.front else body).rstrip() + "\n"

    @property
    def body(self) -> str:
        return self.render()[len(self.front):].strip() if self.front else self.render()


def detect(path) -> str:
    suffix = str(path).rsplit(".", 1)[-1].lower()
    return suffix if suffix in FORMATS else "md"


def _label(head: str) -> str:
    return re.sub(r"<[^>]+>|^#+\s*", "", head).strip()


def parse(text: str, fmt: str) -> Doc:
    if fmt == "html":
        return _parse_html(text)
    return _parse_md(text)


def _parse_md(text: str) -> Doc:
    m = re.match(r"^(---\n.*?\n---\n)(.*)$", text, re.S)
    front, body = (m.group(1), m.group(2)) if m else ("", text)

    sections, head, buf = [], "", []
    for line in body.split("\n"):
        if line.startswith("## "):
            sections.append(Section(head, "\n".join(buf).strip(), _label(head)))
            head, buf = line, []
        else:
            buf.append(line)
    sections.append(Section(head, "\n".join(buf).strip(), _label(head)))
    return Doc("mdx" if front else "md", front,
               [s for s in sections if s.head or s.body])


def _parse_html(text: str) -> Doc:
    """Режем по <h2>. Всё, что до первого <h2>, — вводный блок без заголовка."""
    parts = re.split(r"(?=<h2[ >])", text)
    sections = []
    for part in parts:
        if not part.strip():
            continue
        m = re.match(r"(<h2[^>]*>.*?</h2>)(.*)$", part, re.S)
        if m:
            sections.append(Section(m.group(1).strip(), m.group(2).strip(),
                                    _label(m.group(1))))
        else:
            sections.append(Section("", part.strip(), ""))
    return Doc("html", "", sections)


# ── что запрещено менять: считаем и сверяем до/после ──────────────────────────
def markup_signature(body: str, fmt: str) -> dict:
    """Отпечаток разметки. Совпал до и после — структуру не сломали."""
    if fmt == "html":
        tags = re.findall(r"<(\w+)[ >]", body)
        return {f"<{t}>": tags.count(t) for t in sorted(set(tags))}
    lines = body.split("\n")
    return {
        "списки": sum(1 for ln in lines if re.match(r"^\s*[-*]\s", ln)),
        "нумерованные": sum(1 for ln in lines if re.match(r"^\s*\d+\.\s", ln)),
        "строки таблицы": sum(1 for ln in lines if ln.strip().startswith("|")),
        "жирные маркеры": len(re.findall(r"\*\*[^*]+\*\*", body)),
    }
