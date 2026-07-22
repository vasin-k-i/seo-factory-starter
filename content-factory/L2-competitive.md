# L2 — реверс-инжиниринг выдачи (опциональный модуль)

Перед написанием статьи разобрать **реально ранжирующийся топ-10 Яндекса** по целевому
запросу и отдать писателю «чертёж»: целевой объём, число картинок, нужны ли таблицы/
инфографика, LSI-слова и подтемы конкурентов. Задача — не выдумывать, а **дотянуть и
превзойти консенсус выдачи**. Дефолт — ВЫКЛ (`L2_COMPETITIVE=0`), включается как опция.

## Что в комплекте
`competitive.py` — самодостаточный модуль (все импорты ленивые, любая ошибка → пусто →
генерация тихо идёт как обычно, без разбора). Ключевые функции:
- `serp_top(query, n)` — top-N URL выдачи. Провайдеры: **SerpApi** (если задан `SERPAPI_KEY`)
  → иначе **Yandex Cloud Search API** (см. ниже, без доп. подписки).
- `collect_competitive(topic)` — SERP → выкинуть свои домены → разобрать до `COMPETITIVE_N`
  конкурентов (title/H2/объём/картинки/таблицы/инфографика/текст). `topic.primary_keyword`.
- `competitive_user(topic, sources)` — бриф для стадии генерации (метрики+LSI+тексты).
- `serp_consensus_block(query, secondary)` — готовый компактный блок «ориентиры по топ-10»
  (медиана объёма, картинки, LSI, подтемы) для вставки в контекст/промпт писателя.

## Как включить
1. **Флаг**: `workflow_dispatch → l2_competitive=1` (или `L2_COMPETITIVE=1` в env шага).
2. **Свой домен**: `SITE_DOMAIN=mysite.ru` (через запятую можно несколько) — исключить себя
   из разбора.
3. **Источник SERP** — одно из:
   - `SERPAPI_KEY` (SerpApi, engine=yandex), **или**
   - `YANDEX_CLOUD_API_KEY` + `YC_FOLDER_ID` — **Yandex Cloud Search API** (тот же сервис-
     аккаунтный ключ, что и Wordstat; у ключа должен быть scope веб-поиска). Эндпоинт
     `POST /v2/web/searchAsync` → операция → poll → base64-XML (реализовано в competitive.py).
     Квота веб-поиска отдельная (по умолчанию 100/час). Оплата pay-per-use.
4. **Зависимости**: `beautifulsoup4` (уже в `requirements.txt`).

Тонкие ручки (env): `COMPETITIVE_N` (глубина разбора, деф. 3), `SERP_TOP_N` (сколько URL брать
из выдачи, деф. 10).

## Как подключить в генерацию (2 правки в orchestrator.py)
Модуль готов, осталось прокинуть его результат в промпт писателя. Мини-паттерн:

```python
# 1) флаг рядом с остальными env
L2_ENABLED = os.environ.get("L2_COMPETITIVE", "0").lower() in ("1", "true")

# 2) в process_topic() перед генерацией:
brief_hint = ""
if L2_ENABLED:
    try:
        import competitive as comp
        brief_hint = comp.serp_consensus_block(topic.primary_keyword)
    except Exception:
        brief_hint = ""
# ...добавить brief_hint в user-текст стадии брифа/писателя (как отдельную секцию,
#   пометив «ОРИЕНТИРЫ ПО СТРУКТУРЕ, не факты для утверждений»).
```

Полностью проваренный референс (Batch API, стадия `00_competitive`, картинки) — в боевом
`college-site/content-factory/orchestrator.py` (функции `_collect_competitive_one`,
`brief_user(..., competitive)`), там L2 включён на cron.

## Важно (анти-спам, как и весь starter)
Это **grey-hat white-box**: мы лишь читаем публичную выдачу и подгоняем свой контент под
консенсус. Никакой накрутки ПФ, кросс-линковки своих сайтов и авто-линкбилдинга — см. общие
директивы проекта.
