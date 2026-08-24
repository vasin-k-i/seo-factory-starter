#!/usr/bin/env python3
"""Адаптер IR-формата psycholesson: статья — это JSON с массивом типизированных блоков.

Чем отличается от markdown-сайтов флота. Там статья — файл с текстом, и рерайт можно
гнать кусками произвольной длины. Здесь статья это дерево:

    {"title": …, "slug": …, "blocks": [
        {"type": "heading", "text": …, "anchor": …},
        {"type": "paragraph", "html": …},
        {"type": "list", "items": [...]},
        {"type": "faq", "items": [{"q": …, "a": …}]}, …]}

Дальше `publish.py` засевает это в Payload CMS. Поэтому подход другой:

  • Модели отдаётся ТОЛЬКО массив строк, и она обязана вернуть массив ТОЙ ЖЕ ДЛИНЫ.
    Сборка идёт по индексам, а не по разметке — структуру сломать физически нельзя.
  • Заголовки, якоря, слаг, SEO-поля и оглавление НЕ отдаются вообще. `anchor` в heading
    совпадает с `anchor` в блоке toc: разъедутся — сломается навигация по статье.
  • title/seo_title/seo_description/excerpt тоже не трогаем: это короткие строки,
    маркировка на такой длине статистически не живёт, а риск испортить SEO реальный.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

# Что можно переписывать: (тип блока, путь до текста внутри блока).
# Всё, чего здесь нет, — неприкосновенно.
REWRITABLE = {
    "paragraph": ["html"],
    "lead": ["html", "text"],
    "highlight": ["html", "text"],
}
LIST_BLOCKS = {"list": "items", "numbered": "items"}
PAIR_BLOCKS = {"faq": ("q", "a"), "features": ("title", "text")}
# Заголовки, оглавление и всё остальное просто не попадает в слоты.


@dataclass
class Slot:
    """Одно место в дереве, куда можно вернуть переписанный текст."""
    block: int
    path: tuple                  # ("html",) | ("items", 3) | ("items", 2, "a")

    def get(self, blocks: list) -> str:
        node = blocks[self.block]
        for key in self.path:
            node = node[key]
        return node if isinstance(node, str) else ""

    def set(self, blocks: list, value: str) -> None:
        node = blocks[self.block]
        for key in self.path[:-1]:
            node = node[key]
        node[self.path[-1]] = value


@dataclass
class IRSection:
    label: str
    slots: list = field(default_factory=list)


@dataclass
class IRDoc:
    data: dict
    sections: list = field(default_factory=list)

    @property
    def blocks(self) -> list:
        return self.data["blocks"]

    def text_of(self, section: IRSection) -> str:
        return "\n\n".join(s.get(self.blocks) for s in section.slots)

    @property
    def body(self) -> str:
        """Плоский текст статьи — для гейтов и аудита."""
        return "\n\n".join(self.text_of(s) for s in self.sections if s.slots)

    def render(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"


def parse(text: str) -> IRDoc:
    data = json.loads(text)
    doc = IRDoc(data, [])
    current = IRSection("лид", [])

    for i, block in enumerate(data.get("blocks", [])):
        btype = block.get("type") or block.get("blockType") or ""

        if btype == "heading":
            if current.slots:
                doc.sections.append(current)
            current = IRSection(block.get("text", "") or "раздел", [])
            continue                                    # заголовок не переписываем

        if btype in REWRITABLE:
            for key in REWRITABLE[btype]:
                if isinstance(block.get(key), str) and block[key].strip():
                    current.slots.append(Slot(i, (key,)))
        elif btype in LIST_BLOCKS:
            key = LIST_BLOCKS[btype]
            for j, item in enumerate(block.get(key) or []):
                if isinstance(item, str) and item.strip():
                    current.slots.append(Slot(i, (key, j)))
                elif isinstance(item, dict):
                    for sub in ("text", "html"):
                        if isinstance(item.get(sub), str) and item[sub].strip():
                            current.slots.append(Slot(i, (key, j, sub)))
        elif btype in PAIR_BLOCKS:
            # у FAQ переписываем только ОТВЕТЫ: вопрос — это поисковый запрос,
            # он же уходит в FAQPage-разметку, менять его нельзя
            _, answer_key = PAIR_BLOCKS[btype]
            for j, item in enumerate(block.get("items") or []):
                if isinstance(item, dict) and isinstance(item.get(answer_key), str) \
                        and item[answer_key].strip():
                    current.slots.append(Slot(i, ("items", j, answer_key)))

    if current.slots:
        doc.sections.append(current)
    return doc


def texts(doc: IRDoc, section: IRSection) -> list[str]:
    return [s.get(doc.blocks) for s in section.slots]


def apply(doc: IRDoc, section: IRSection, values: list[str]) -> None:
    if len(values) != len(section.slots):
        raise ValueError(
            f"вернулось {len(values)} кусков вместо {len(section.slots)} — "
            f"сборка по индексам невозможна")
    for slot, value in zip(section.slots, values):
        slot.set(doc.blocks, value)
