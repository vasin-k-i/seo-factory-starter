#!/usr/bin/env python3
"""Пайплайн стирания маркировки: рерайт → механический гейт → аудит → точечная правка → приёмка.

    Claude написал  →  [1] рерайтер пересказывает посекционно
                    →  [2] механический гейт: числа, ссылки, разметка, объём
                    →  [3] Claude ЧИТАЕТ и выдаёт список смысловых дефектов
                    →  [4] рерайтер чинит ТОЧЕЧНО по ТЗ аудита (не переписывает заново)
                    →  [5] Claude принимает: есть грубые дефекты — не публикуем
                    →  публикация

Почему именно так, а не «прогнали через DeepSeek и в прод». Замер 23.08.2026 на боевой
статье ped-college: DeepSeek ломает 91,5 % шестисловных цепочек и не теряет ни числа,
ни ссылки, ни разметку — но тихо вносит смысловые подмены. На той статье их было пять,
включая перевёрнутое отношение «часть — целое» и выдуманных врачей в детском саду.
Механический гейт на них не среагировал: с его точки зрения всё чисто.

Отсюда два жёстких правила:
  • ПРАВИТ только не-Claude. Любая правка Клодом возвращает маркировку.
  • ПРИНИМАЕТ только Claude. Он читает и выносит вердикт, в текст не попадает.

Шаг 4 — точечный: чинятся ТОЛЬКО секции с дефектами и ТОЛЬКО перечисленные дефекты.
Полный повторный рерайт вносил бы новые подмены вместо старых, и так до бесконечности.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from . import backends, formats, gates

REWRITE_SYSTEM = """Ты — редактор блога. Работаешь по «Пиши, сокращай» Ильяхова:
информационный стиль, без воды и канцелярита.

Тебе дают готовый, уже опубликованный кусок статьи. Задача узкая и необычная:
СКАЗАТЬ РОВНО ТО ЖЕ САМОЕ ДРУГИМИ СЛОВАМИ. Не улучшить, не дополнить, не сократить."""

REWRITE_RULES = """ПРАВИЛА. Нарушение любого — брак.

1. СМЫСЛ НЕ МЕНЯЕТСЯ. Каждое утверждение исходника остаётся. Ничего не выбрасывай и
   ничего не добавляй — ни фактов, ни примеров, ни выводов. Новых цифр не появляется
   НИКОГДА. Не уточняй то, что в исходнике сказано обобщённо: «медицинские работники»
   не превращаются во «врачей», «один-два дня» не превращаются в «полтора дня».

2. НЕ ПЕРЕВОРАЧИВАЙ ОТНОШЕНИЯ. Если в исходнике «А входит в состав Б», то и у тебя
   А входит в Б, а не наоборот. Это самая частая и самая дорогая ошибка пересказа.

3. НЕ МЕНЯЙ СИЛУ УТВЕРЖДЕНИЯ. «Обычно» не становится «всегда», «может» не становится
   «обязан», «список тем» не становится «перечнем вопросов».

4. ФОРМУЛИРОВКИ МЕНЯЮТСЯ ВСЕ. Другой порядок слов, другие связки, другая длина фраз.
   Совпадение с исходником длиннее пяти слов подряд недопустимо — кроме пункта 5.

5. ДОСЛОВНО переносятся: все числа, даты, сроки, суммы; термины и аббревиатуры;
   названия законов, приказов, документов, организаций, должностей; ссылки целиком —
   и адрес, и якорный текст.

6. РАЗМЕТКА ОДИН В ОДИН. Столько же пунктов списка, столько же шагов, та же таблица
   с теми же строками, те же теги, столько же абзацев. Жирные маркеры — дословно.

7. ИНФОСТИЛЬ. Запрещены «является», «осуществляет», «представляет собой», «в рамках»,
   «в настоящее время», «следует отметить», «таким образом», «данный». Запрещены
   эпитеты «качественный», «эффективный», «современный», «уникальный». Активный залог.
   Без эмодзи. Средняя длина предложения 12–18 слов.

8. ОБЪЁМ ±10 % от исходного.

ОТВЕТ — только переписанный текст. Без преамбул, без пояснений, без ``` вокруг.
Не проговаривай свой план и не комментируй процесс — всё это попадёт в статью."""

AUDIT_PROMPT = """Сверь ОРИГИНАЛ статьи и её ПЕРЕСКАЗ другой моделью.

Пересказ должен был сказать ровно то же самое другими словами. Найди места, где смысл
поехал. Синонимы, другой порядок слов и другая длина фраз — это НОРМА, их меняли
намеренно, к ним не придирайся.

Ищи только смысловые дефекты:
- перевёрнутые отношения («А входит в Б» стало «Б входит в А»);
- изменённую силу утверждения («обычно» → «всегда», «может» → «обязан»);
- потерянное условие или оговорку;
- факт, которого в оригинале не было (выдуманная конкретика, лишние подробности);
- утраченный факт;
- служебные реплики модели, попавшие в текст статьи.

Верни ТОЛЬКО JSON, без пояснений вокруг:

{"defects": [
  {"section": "<точный заголовок раздела или \\"лид\\" для вводного блока>",
   "severity": "критично|заметно|мелочь",
   "original": "<короткая цитата оригинала>",
   "rewrite": "<короткая цитата пересказа>",
   "what": "<в чём подмена, одной фразой>",
   "fix": "<что именно сделать, чтобы починить, одной фразой>"}
]}

Дефектов нет — верни {"defects": []}."""

PATCH_SYSTEM = """Ты — редактор. Тебе дают кусок текста и список КОНКРЕТНЫХ дефектов в нём.
Почини ровно эти дефекты и больше ничего не трогай."""

VERDICT_PROMPT = """Прими работу. Сверь ОРИГИНАЛ и ИТОГОВЫЙ ТЕКСТ после пересказа и правок.

Вопрос один: можно ли публиковать? Публиковать нельзя, если остались смысловые подмены
уровня «критично» — перевёрнутые отношения, выдуманные факты, изменённая сила утверждений,
потерянные условия, служебные реплики модели в тексте. Стилистика и синонимы значения не имеют.

Верни ТОЛЬКО JSON:
{"verdict": "PASS|FAIL", "critical": ["<оставшийся грубый дефект>", ...], "note": "<одна фраза>"}"""


@dataclass
class Result:
    path: str
    status: str                     # published | rejected | skipped | error
    reason: str = ""
    stats: dict = field(default_factory=dict)
    defects: list = field(default_factory=list)
    critical: list = field(default_factory=list)
    cost_rub: float = 0.0
    seconds: float = 0.0
    text: str | None = None         # итоговый текст; None — писать нечего


def _rewrite_section(rewriter, section: formats.Section, fmt: str, log) -> str:
    """Один проход по секции + до двух повторов, если механический гейт ругнулся."""
    where = f"Раздел «{section.label}»." if section.label else "Вводный блок статьи (лид)."
    fmt_note = ("Текст в формате HTML: все теги переносятся без изменений."
                if fmt == "html" else "Текст в формате Markdown.")
    base = (f"{REWRITE_RULES}\n\n{where} {fmt_note}\n"
            f"В исходнике {len(gates.words(section.body))} слов — держись объёма.\n\n"
            f"ИСХОДНЫЙ ТЕКСТ:\n\n{section.body}")

    best, best_problems = section.body, ["не переписан"]
    for attempt in range(3):
        prompt = base if attempt == 0 else (
            base + "\n\nПРЕДЫДУЩАЯ ПОПЫТКА ЗАБРАКОВАНА: "
            + "; ".join(best_problems) + ". Учти это и перепиши заново.")
        try:
            out = backends.strip_fence(rewriter(REWRITE_SYSTEM, prompt))
        except Exception as e:                       # noqa: BLE001
            log(f"      попытка {attempt + 1}: {type(e).__name__}: {e}")
            time.sleep(3 * (attempt + 1))
            continue
        problems = gates.check_section(section.body, out, fmt)
        if not problems:
            return out
        log(f"      попытка {attempt + 1} забракована: {'; '.join(problems)}")
        best, best_problems = out, problems
    # три раза не смогли — честнее оставить оригинал, чем публиковать поломанное
    log(f"      ⚠️ секция оставлена как была: {'; '.join(best_problems)}")
    return section.body


def _patch_section(rewriter, body: str, defects: list, fmt: str, log) -> str:
    tz = "\n".join(
        f"{i}. Сейчас: «{d.get('rewrite', '')}». Должно быть по смыслу: «{d.get('original', '')}». "
        f"Проблема: {d.get('what', '')}. Что сделать: {d.get('fix', '')}"
        for i, d in enumerate(defects, 1))
    prompt = (
        f"Формат текста: {'HTML, теги не трогать' if fmt == 'html' else 'Markdown'}.\n\n"
        f"ДЕФЕКТЫ, которые нужно починить:\n{tz}\n\n"
        "ПРАВИЛА ПРАВКИ:\n"
        "- Меняй ТОЛЬКО те фразы, которые названы в списке. Остальной текст оставь "
        "символ в символ — он уже принят.\n"
        "- Не переписывай раздел заново: полная переделка внесёт новые подмены "
        "вместо старых.\n"
        "- Разметку, числа, ссылки и объём не трогай.\n"
        "- Не возвращай формулировки оригинала дословно: почини смысл СВОИМИ словами, "
        "иначе вернётся то, ради чего затевался пересказ.\n\n"
        f"ТЕКСТ ДЛЯ ПРАВКИ:\n\n{body}")
    try:
        out = backends.strip_fence(rewriter(PATCH_SYSTEM, prompt))
    except Exception as e:                           # noqa: BLE001
        log(f"      правка не удалась: {type(e).__name__}: {e}")
        return body
    problems = gates.check_section(body, out, fmt)
    # здесь «переписано мало» — норма: правка точечная, а не пересказ
    problems = [p for p in problems if "переписано всего" not in p]
    if problems:
        log(f"      правка забракована гейтом: {'; '.join(problems)}; оставляю как было")
        return body
    return out


def process(path, *, rewriter_name: str = "deepseek", log=print,
            dry: bool = False) -> Result:
    started = time.time()
    backends.reset_usage()
    path = str(path)
    fmt = formats.detect(path)

    try:
        rewriter = backends.REWRITERS[rewriter_name]
    except KeyError:
        return Result(path, "error", f"нет рерайтера «{rewriter_name}»")

    import pathlib
    original_text = pathlib.Path(path).read_text(encoding="utf-8")
    orig_doc = formats.parse(original_text, fmt)
    log(f"  {pathlib.Path(path).name}: {len(orig_doc.sections)} секций, "
        f"{len(gates.words(orig_doc.body))} слов, рерайтер {rewriter_name}")

    # [1]+[2] рерайт с механическим гейтом на каждой секции
    new_doc = formats.Doc(orig_doc.fmt, orig_doc.front, [])
    for i, sec in enumerate(orig_doc.sections, 1):
        log(f"    [{i}/{len(orig_doc.sections)}] {sec.label or 'лид'}")
        body = _rewrite_section(rewriter, sec, fmt, log) if sec.body.strip() else sec.body
        new_doc.sections.append(formats.Section(sec.head, body, sec.label))

    # [3] аудит смысла — Claude только читает
    log("    аудит смысла (Claude)…")
    try:
        audit = backends.parse_json(backends.call_claude(
            f"{AUDIT_PROMPT}\n\n=== ОРИГИНАЛ ===\n{orig_doc.body}"
            f"\n\n=== ПЕРЕСКАЗ ===\n{new_doc.body}"))
        defects = audit.get("defects", []) if isinstance(audit, dict) else []
    except Exception as e:                           # noqa: BLE001
        return Result(path, "error", f"аудит не отработал: {type(e).__name__}: {e}",
                      seconds=time.time() - started, cost_rub=backends.cost_rub())

    log(f"    дефектов: {len(defects)}"
        + (f" (критичных {sum(1 for d in defects if d.get('severity') == 'критично')})"
           if defects else ""))

    # [4] точечная правка — только затронутые секции, только названные дефекты
    by_section: dict[str, list] = {}
    for d in defects:
        if d.get("severity") == "мелочь":
            continue                                 # мелочи не трогаем: правка рискованнее дефекта
        by_section.setdefault((d.get("section") or "").strip().lower(), []).append(d)

    unfixed_critical: list[str] = []
    if by_section:
        for sec in new_doc.sections:
            key = (sec.label or "лид").strip().lower()
            hits = by_section.get(key) or (by_section.get("лид") if not sec.label else None)
            if not hits:
                continue
            log(f"    правлю «{sec.label or 'лид'}»: {len(hits)} дефектов")
            before = sec.body
            sec.body = _patch_section(rewriter, sec.body, hits, fmt, log)
            if sec.body == before:
                unfixed_critical += [d.get("what", "без описания") for d in hits
                                     if d.get("severity") == "критично"]

    # Дефект в секции, которую аудит назвал, но которой нет в документе (модель
    # переврала заголовок), — тоже не починен. Молча терять такие нельзя.
    matched = {(s.label or "лид").strip().lower() for s in new_doc.sections}
    for key, hits in by_section.items():
        if key not in matched and key != "лид":
            unfixed_critical += [d.get("what", "без описания") for d in hits
                                 if d.get("severity") == "критично"]

    # [5] приёмка — снова только чтение
    log("    приёмка (Claude)…")
    try:
        verdict = backends.parse_json(backends.call_claude(
            f"{VERDICT_PROMPT}\n\n=== ОРИГИНАЛ ===\n{orig_doc.body}"
            f"\n\n=== ИТОГОВЫЙ ТЕКСТ ===\n{new_doc.body}"))
    except Exception as e:                           # noqa: BLE001
        return Result(path, "error", f"приёмка не отработала: {type(e).__name__}: {e}",
                      seconds=time.time() - started, cost_rub=backends.cost_rub())

    stats = gates.check_document(orig_doc, new_doc)
    critical = verdict.get("critical", []) if isinstance(verdict, dict) else []
    passed = isinstance(verdict, dict) and verdict.get("verdict") == "PASS"

    # Гейт важнее вердикта модели: это красные линии, а не мнение.
    if not stats["заголовки_целы"]:
        passed, critical = False, critical + ["сбиты заголовки разделов"]
    if stats["ссылки_потеряны"]:
        passed, critical = False, critical + [
            f"потеряны ссылки: {', '.join(stats['ссылки_потеряны'])}"]
    if stats["числа_потеряны"]:
        passed, critical = False, critical + [
            f"потеряны числа: {', '.join(stats['числа_потеряны'])}"]

    # Главная проверка, ради которой всё затевалось. Если рерайт не смог оторваться
    # от оригинала, публиковать нечего: маркировка на месте, а деньги потрачены.
    # Так бывает на плотном техническом тексте — термины, ГОСТы и марки металла
    # держат формулировки, и переписать их «другими словами» просто нечем.
    if stats["переписано_%"] < gates.MIN_REWRITE_DOC:
        passed = False
        critical = critical + [
            f"переписано всего {stats['переписано_%']}% при пороге "
            f"{gates.MIN_REWRITE_DOC}% — маркировка уцелела, смысла публиковать нет"]

    # Дефект, который аудит назвал критичным, а правка не смогла починить, —
    # это не «ну и ладно»: именно на таких текст едет тихо и незаметно.
    if unfixed_critical:
        passed = False
        critical = critical + [f"не починен критичный дефект: {d}" for d in unfixed_critical]

    result = Result(
        path=path,
        status="published" if passed else "rejected",
        reason=(verdict.get("note", "") if isinstance(verdict, dict) else ""),
        stats=stats, defects=defects, critical=critical,
        cost_rub=backends.cost_rub(), seconds=time.time() - started,
        text=new_doc.render() if passed else None)

    if passed and not dry:
        pathlib.Path(path).write_text(result.text, encoding="utf-8")
    log(f"    → {result.status.upper()}: переписано {stats['переписано_%']}%, "
        f"{result.cost_rub:.2f} ₽, {result.seconds:.0f} с"
        + (f"; блокеры: {'; '.join(critical)}" if critical else ""))
    return result


def to_json(results: list[Result]) -> str:
    return json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=2)
