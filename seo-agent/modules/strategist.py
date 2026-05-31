"""
SEO-стратег — человеческие рекомендации поверх сухих данных дайджеста.

Берёт payload ежедневного/недельного дайджеста (трафик, позиции, тех-аудит,
Вебмастер, публикации) и просит Claude сформулировать 3–5 приоритетов на сегодня
живым языком: что улучшить, над чем поработать. Без жаргона, без кодов модулей.

Запускается в GitHub Actions (вне РФ — Anthropic API доступен). Если ключа нет
или SDK не установлен — молча возвращает пустую строку, отчёт всё равно уходит.

ENV:
    ANTHROPIC_API_KEY — тот же ключ, что у content-factory / M1.
    ANTHROPIC_MODEL   — по умолчанию claude-sonnet-4-6.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — опытный SEO-стратег, который ведёт сайт клиента и каждое утро пишет ему "
    "короткую записку: на что обратить внимание сегодня. Клиент — маркетолог, "
    "не разработчик. Пиши по-русски, живым человеческим языком, без англицизмов "
    "и без технического жаргона (никаких CWV, canonical, query, snapshot — если "
    "упоминаешь техническую вещь, тут же объясняй простыми словами). "
    "Опирайся ТОЛЬКО на приведённые данные, не выдумывай цифр. "
    "Дай 3–5 конкретных приоритетов в порядке важности. Каждый пункт — "
    "одно-два предложения: что сделать и зачем (какой результат это даст). "
    "Если данных мало или всё в порядке — так и скажи, не раздувай. "
    "Формат ответа — только нумерованный список, без вступления и заключения, "
    "без markdown-заголовков."
)


def _payload_to_brief(site: str, period: str, payload: dict) -> str:
    """Сжать payload в короткий текстовый бриф для модели."""
    lines = [f"Сайт: {site}", f"Период: {period}", ""]

    t = payload.get("traffic") or {}
    if t:
        lines.append(
            f"Трафик из Google: переходов {t.get('clicks')} "
            f"(изменение {t.get('delta_clicks'):+d}), "
            f"показов {t.get('impressions')} (изменение {t.get('delta_impressions'):+d})."
        )

    yw = payload.get("yandex_webmaster") or {}
    if yw.get("sqi") is not None:
        sqi_delta = yw.get("sqi_delta")
        d = f" (изменение {sqi_delta:+d})" if isinstance(sqi_delta, int) else ""
        lines.append(f"Индекс качества сайта в Яндексе (ИКС): {yw['sqi']}{d}.")
    if yw.get("index_count") is not None:
        lines.append(f"Страниц в индексе Яндекса: {yw['index_count']}.")

    r = payload.get("rankings") or {}
    if r:
        lines.append(
            f"Позиции: в ТОП-10 Google {r.get('google_top10', 0)} из "
            f"{r.get('total', 0)} запросов, в ТОП-30 {r.get('google_top30', 0)}."
        )
        for label, key in (("Вышли на 1-ю страницу", "new_top10"),
                            ("Заметно выросли", "risers"),
                            ("Заметно просели", "fallers")):
            items = r.get(key) or []
            if items:
                ex = "; ".join(f"«{kw}» {old:.0f}→{new:.0f}" for kw, old, new, _ in items[:5])
                lines.append(f"{label} ({len(items)}): {ex}.")

    a = payload.get("audit") or {}
    issues = a.get("issues", []) if a else []
    if a:
        by_sev: dict[str, int] = {}
        for i in issues:
            by_sev[i["severity"]] = by_sev.get(i["severity"], 0) + 1
        urgent = by_sev.get("critical", 0) + by_sev.get("high", 0)
        minor = by_sev.get("medium", 0) + by_sev.get("low", 0)
        new_count, fixed_count = payload.get("audit_delta", (0, 0))
        lines.append(
            f"Технические замечания: важных {urgent}, мелких {minor}. "
            f"За период новых {new_count}, исправлено {fixed_count}."
        )
        # Самые частые типы проблем — даём модели контекст для конкретики.
        from collections import Counter
        try:
            from notifiers.humanize import check_ru
        except Exception:
            def check_ru(c: str) -> str:
                return c.replace("_", " ")
        top_checks = Counter(i["check"] for i in issues).most_common(6)
        if top_checks:
            lines.append("Чаще всего встречается: "
                         + "; ".join(f"{check_ru(c)} ×{n}" for c, n in top_checks) + ".")
        stats = a.get("crawl_stats") or {}
        broken = (stats.get("client_err", 0) or 0) + (stats.get("server_err", 0) or 0)
        if broken:
            lines.append(f"Недоступных/битых страниц: {broken} из {stats.get('total')}.")

    cf = payload.get("content_published") or []
    if cf:
        lines.append(f"Опубликовано новых статей за период: {len(cf)}.")

    s = payload.get("semantic") or {}
    if s.get("added_total"):
        lines.append(f"Добавлено новых тем для статей: {s['added_total']}.")

    return "\n".join(lines)


def strategist_advice(site: str, period: str, payload: dict,
                      max_points: int = 5) -> str:
    """Вернуть текст рекомендаций (нумерованный список) или "" при недоступности."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.info("ANTHROPIC_API_KEY не задан — блок стратега пропущен")
        return ""

    try:
        import anthropic
    except ImportError:
        log.warning("anthropic SDK не установлен — блок стратега пропущен")
        return ""

    brief = _payload_to_brief(site, period, payload)
    user_prompt = (
        "Вот данные по продвижению сайта за период:\n\n"
        f"{brief}\n\n"
        f"Сформулируй до {max_points} приоритетов на сегодня по правилам из инструкции."
    )

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            max_tokens=1200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        log.warning("Claude (стратег) недоступен: %s", e)
        return ""

    text = response.content[0].text.strip() if response.content else ""
    # На всякий случай снимаем markdown-обёртку, если модель её добавила.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()
