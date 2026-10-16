# GameAnalytics

Собирает выходящие игры с Metacritic каждый час, хранит карточки и все отзывы и показывает их в веб-интерфейсе. DevPanel показывает работу пайплайна в реальном времени.

Архитектура и решения: [docs/architecture.html](docs/architecture.html).

## Что уже работает

- Часовой цикл с пустой базы, без предварительной загрузки: сначала 20 игр блока New Releases с metacritic.com/game/ (тот же запрос, что у сайта; обрабатываются первыми, место в топе хранится у игры и снимком за день — `GET /api/top`), затем каталог и New Releases за последние три дня целиком, со сверкой с числом игр в API и без дублей; обновление игр по уровням hot / warm / cool / dormant / архив. Раз в сутки — окно за две недели для игр, добавленных задним числом.
- Сбор карточки и всех отзывов критиков и игроков по каждой платформе, с дельтой при повторных проверках.
- API: список с поиском, фильтром по платформам и сортировкой; карточка игры; отзывы; запуск циклов; поток событий (SSE).
- Интерфейс: список игр, карточка, DevPanel.

- LLM-анализ на gemini-3.5-flash-lite: разборы критиков и игроков, суть игры. Первое чтение — до 300 содержательных отзывов (от 50 слов, без дублей, пропорционально оценкам) со сводкой всех оценок; дальше раз в сутки инкремент — прошлый разбор плюс только новые отзывы, под жёсткими лимитами списков. Повтор запускается от прироста отзывов с текстом (+20% или +25, минимум 3), не чаще раза в сутки. Включается `GA_LLM_PROVIDER=gemini` и `GA_GEMINI_API_KEY` в `backend/.env`.

Ещё не подключено: эмбеддинги для похожих игр и Deep analyze. Сейчас похожие игры подбираются по жанру.

## Запуск для разработки

```bash
docker compose up -d postgres redis
```

```bash
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

API (http://localhost:8000):

```bash
cd backend && .venv/bin/uvicorn app.api.main:app --reload
```

Воркер с часовым расписанием:

```bash
cd backend && .venv/bin/arq app.pipeline.tasks.WorkerSettings
```

Интерфейс (http://localhost:5173):

```bash
cd frontend && npm install && npm run dev
```

Проанализировать игры вручную, без триггера:

```bash
cd backend && .venv/bin/python -m app.cli analyze control-resonant marvels-wolverine
```

Часовой цикл без воркера, прямо в терминале:

```bash
cd backend && .venv/bin/python -m app.cli inline hourly
```

Загрузка New Releases за N дней (`inline bootstrap --days N`) осталась как ручная команда, сам цикл её не запускает.

## Всё в Docker

```bash
docker compose up -d --build
```

Интерфейс откроется на http://localhost:8080, API на http://localhost:8000.

## Тесты

```bash
cd backend && .venv/bin/pytest
```

Живые проверки против Metacritic: контракт ответа и тайминг 20 карточек со всеми отзывами.

```bash
cd backend && GA_LIVE=1 .venv/bin/pytest tests/timing -s
```

## Настройки

Все параметры в [backend/app/core/config.py](backend/app/core/config.py), любой можно переопределить переменной `GA_*` (пример в `backend/.env.example`): лимит запросов, окно первого запуска, пороги уровней и повторного анализа.
