# Реестр источников и уровни доверия

Собрано 2026-08-11. Дальше по тексту системы каждое правило помечено уровнем доказанности.

## Шкала

| Уровень | Что это | Как обращаться |
|---|---|---|
| **A** | Большая выборка с раскрытой методикой или контролируемый эксперимент. Baymard, NN/g, Zuko, Unbounce, web.dev, ЦБ, Яндекс+ОРО | Можно опираться как на факт |
| **B** | Агрегированные бенчмарки вендора по своей платформе (Interact, Riddle, Goldcast, Popupsmart, First Page Sage) | Порядок величины верный, абсолют — нет. Вендор мерит своих клиентов |
| **C** | Практика агентств и экспертов без публичной методики (транскрипты роликов, кейсы студий) | Гипотеза для теста, не аргумент в споре |
| **X** | Заявление продавца о собственном продукте, цифру проверить нельзя | В систему не берём. Помечаем и игнорируем |

⚠️ Отдельно: RU-выдача по запросам «средняя конверсия лендинга по отраслям 2025/2026» на 80% забита
SEO-фабриками с сочинёнными таблицами (growmatrix, dipustovalov, rechka и т.п.). Ни одна такая цифра
в систему не взята. RU-бенчмарки берём только там, где есть первоисточник (ЦБ, Яндекс, Data Insight)
или где это наблюдаемый факт (разбор живого лендинга).

## A — первичные исследования

| Источник | Что дал | Выборка / дата |
|---|---|---|
| [Unbounce Conversion Benchmark Report](https://unbounce.com/conversion-benchmark-report/) | Медианы конверсии, корреляции текста | 57 млн конверсий, 41 тыс. лендингов, данные 2024 |
| [Unbounce — Education](https://unbounce.com/conversion-benchmark-report/education-conversion-rate/) | Бенчмарки образования, целевой объём текста | тот же датасет |
| [Baymard — cart abandonment](https://baymard.com/lists/cart-abandonment-rate) | 70,22% брошенных корзин + причины | 50 исследований, обновл. 22.09.2025 |
| [Baymard — Mobile UX Trends 2026](https://baymard.com/blog/mobile-ux-ecommerce) | 10 практик + % сайтов, которые их не делают | бенчмарк ведущих US/EU сайтов, 2026 |
| [Baymard — Ecommerce Quantitative UX 2026](https://baymard.com/blog/ecommerce-quantitative-ux-insights-2026) | Приоритеты покупателей, отписки | опрос 1083 покупателей США, 2026 |
| [Zuko — industry benchmarking](https://www.zuko.io/benchmarking/industry-benchmarking) | Конверсия форм, поля-убийцы, device gap | 93 022 997 сессий форм |
| [NN/g — Web Form Design](https://www.nngroup.com/articles/web-form-design/) | 10 правил форм | UX-исследование |
| [NN/g — EAS Framework](https://www.nngroup.com/articles/eas-framework-simplify-forms/) | Eliminate / Automate / Simplify | 2026 |
| [NN/g — 4 principles cognitive load](https://www.nngroup.com/articles/4-principles-reduce-cognitive-load/) | Структура, прозрачность, ясность, поддержка | 2026 |
| [NN/g — 3-Step CTAs](https://www.nngroup.com/videos/3-step-ctas-formula-for-conversion/) | Формула CTA | 03.08.2026 |
| [NN/g — How Users Read on the Web](https://www.nngroup.com/articles/how-users-read-on-the-web/) | 79% сканируют / 16% читают; +124% юзабилити | 1997, многократно воспроизведено |
| [NP Digital — conversion by form fields](https://neilpatel.com/marketing-stats/conversion-rate-by-form-fields/) | Кривая «поля → конверсия» | 404 лендинга, декабрь 2024 |
| [web.dev — business impact of CWV](https://web.dev/case-studies/vitals-business-impact) | 18 кейсов «скорость → деньги» | Google, продакшн-кейсы |
| [Яндекс + ОРО (через РИА)](https://ria.ru/20250916/yandeks-2042152040.html) | Отзывы важнее скидки для ощущения справедливой цены | опрос 1500+ россиян, 09.2025 |
| [152-ФЗ, ст. 9 (КонсультантПлюс)](https://www.consultant.ru/document/cons_doc_LAW_61801/) | Требования к согласию | закон |

## B — вендорские бенчмарки

| Источник | Что дал | Выборка |
|---|---|---|
| [Interact — Quiz Conversion Report](https://www.tryinteract.com/blog/quiz-conversion-rate-report/) | Старт→лид 40,1%, по нишам | 80+ млн лидов с 2013 |
| [Riddle — 2025 Quiz Marketing Report](https://www.riddle.com/blog/news-reviews/2025-quiz-marketing-report/) | Длина квиза, доходимость, CPL | 8,96 млрд точек данных |
| [Goldcast — B2B Webinar Benchmark 2025](https://www.goldcast.io/reports/b2b-webinar-benchmark-report-2025) | Регистрация→приход, длина, заголовки | 19 531 вебинар, 418 брендов |
| [Popupsmart — Popup Benchmark 2025](https://popupsmart.com/blog/popup-conversion-benchmark-report) | Попапы: типы, триггеры, поля | 10k кампаний, 105 млн показов, 01.2024–09.2025 |
| [First Page Sage — SEO Conversion Compendium](https://firstpagesage.com/reports/seo-conversion-statistics-compendium-fc/) | Конверсия по типам контента | 160+ сайтов за 5 лет, обновл. 11.2024 |

## C — практика (YouTube-транскрипты, разобраны целиком)

| Ролик | Канал | Что взято |
|---|---|---|
| COMPLETE CRO Course (2,5 ч) | Exposure Ninja | Фреймворк **CLOSER**, «лур», обработка возражений, формы, продажный процесс после лида |
| How To Create High Converting B2B Landing Pages in 2026 | Exposure Ninja | Разбор 10+ живых B2B-лендингов по CLOSER |
| I Studied 1000 Landing Pages, Here's What Works in 2026 | ThrillX | 7 уроков, формула первого экрана, цифры аплифтов агентства |
| Alex Hormozi's Landing Page Strategy for 2026 | ThrillX + врезки Hormozi | Value Equation на странице, 1 сплит-тест в неделю |
| 3 Landing Page Tests To Skyrocket Conversions | Alex Hormozi | 90% тестов = заголовок или картинка |
| The BEST CRO Tutorial for Ecommerce in 2025 | ThrillX | Аналитика GA4 + Clarity, опросы, review mining, приоритизация тестов |
| Erik Kennedy — Make landing pages that convert (Dive Club) | Dive Club | Дизайн-сторона: заголовок, подзаголовки-смыслы, frame break, «уродливые скриншоты» |
| This Landing Page Has A 79% Conversion Rate | Alex Cattoni | Опт-ин 79%, продажник $27 с CR 14,33%, 60% покупают со второго визита |
| Opt-In Landing Page Anatomy (50%+) | Jon Benson | Анатомия опт-ин страницы, «слепой» опт-ин |
| Optimization by Oliver #50 | ConversionWise | E-commerce микро-правки |
| Рост конверсии в 14 раз. Квиз-сайт юруслуги | А. Дейнека, Парадигма | Мультиверсионные квиз-лендинги, кейс 7% конверсии, 390 ₽ за заявку |
| Почему квиз не работает | А. Дейнека | Разбор квиза с CR 0,1% — список ошибок |
| Разбор лендинга на ошибки 2025 | А. Дейнека | Разнос лендинга ремонта, что читают и что нет |
| Пример продающего лендинга. Конверсия РСЯ 5% | А. Дейнека | Структура «мебельные щиты», мультилендинг, лид-магнит |
| Квиз-сайт строительство. Заявки по 64–108 ₽ | А. Дейнека | 5 версий квиза, A/B по офферам |
| Эти вопросы задает каждый клиент [2025] | А. Дейнека | Конверсия по каналам в РФ, цена лида, дозвон, лендинг vs многостраничник |

## Наблюдения на живых RU-страницах (парсинг)

- `skillbox.ru/course/profession-data-analyst/` — распарсен `scrapling`, анатомия курсового лендинга РФ
  разобрана в `system/06-лендинг-курса.md`.
- `practicum.yandex.ru`, `netology.ru` — отдают заглушку обычному HTTP-клиенту, нужен
  `scrapling extract stealthy-fetch --network-idle`.

## Инструменты сбора в этой сессии

`WebSearch` в сессии не работал (ошибка API). Поиск шёл через `WebFetch` по `lite.duckduckgo.com/lite/?q=`,
парсинг закрытых страниц — `scrapling extract get`, транскрипты — `yt-dlp` c
`--extractor-args "youtube:player_client=android,web_safari,tv"` (без этого «The page needs to be reloaded»).
