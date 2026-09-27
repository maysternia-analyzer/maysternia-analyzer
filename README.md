# Майстерня Аналізатор

Веб-приложение онлайн-школы «Майстерня скілів» (курс «Код Харизми»). Автоматически забирает
записи встреч из Zoom, получает транскрипцию и с помощью Claude оценивает:

- **пробные занятия** — работу тренера по чек-листу из 6 пунктов (приветствие, атмосфера,
  структура, упражнения, обратная связь, переход к менеджеру) + общая оценка и вовлечённость;
- **продажи** — работу менеджера по чек-листу из 5 пунктов (потребность, презентация,
  возражения, срочность, следующий шаг) + шанс сделки, «температура» лида, скрипт следующего
  контакта, топ ошибок.

Результаты — в дашборде: фильтры, статистика по людям, отметка реальных продаж и сумм,
AI-аналитика по всей базе (портрет ЦА, топ потребностей и возражений, рекомендации).

| | |
|---|---|
| Продакшн | https://web-production-420d0.up.railway.app |
| Webhook для Zoom | `https://web-production-420d0.up.railway.app/zoom/webhook` |
| Стек | Python 3.10+, Flask, Gunicorn, PostgreSQL (Railway) / SQLite (локально) |
| AI | Anthropic Claude (`claude-sonnet-4-6`), OpenAI Whisper (опционально) |

Подробности: [архитектура](docs/ARCHITECTURE.md) · [деплой и эксплуатация](docs/OPERATIONS.md) ·
[отчёт аудита 2026-09](docs/AUDIT_2026-09.md).

## Как это работает (коротко)

```
Zoom (облачная запись) ──webhook──►  /zoom/webhook ─┐
        └───── поллер каждые 5 мин (резерв) ────────┤  запись в очереди (БД, status=queued)
Ручная загрузка VTT / TXT / аудио ──────────────────┘
                                                     ▼
                         фоновый воркер (1 процесс-лидер, до 2 задач параллельно)
                         1. транскрипция: VTT от Zoom → текст (секунды)
                                          или аудио → Whisper (если есть ключ OpenAI)
                         2. тип записи и имя ведущего (эвристика / Claude)
                         3. AI-анализ Claude по чек-листу (JSON по схеме)
                                                     ▼
                                    дашборд: status=done + аналитика
```

Очередь хранится в БД, поэтому рестарт/деплой Railway не теряет записи: незавершённые
задачи возвращаются в очередь автоматически, временные ошибки (сеть, 429/5xx) повторяются.

## Быстрый старт локально

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # заполнить ANTHROPIC_API_KEY и SECRET_KEY
python create_admin.py        # первый администратор
python app.py                 # http://localhost:5050
```

Без `DATABASE_URL` используется SQLite (`data.db`). Без ключей Zoom работает только ручная
загрузка; без `OPENAI_API_KEY` — только загрузка готовых транскрипций VTT/TXT.

## Тесты

```bash
pytest -q                                                    # SQLite, все внешние API замоканы
TEST_DATABASE_URL=postgresql://user:pass@localhost/test pytest -q   # те же тесты на PostgreSQL
FFMPEG_TEST_PATH=/usr/bin/ffmpeg pytest tests/test_transcription.py # + реальная нарезка аудио
```

207 тестов: БД и миграции, очередь и восстановление, Zoom API/webhook, конвейер обработки,
AI-слой (ошибки, обрезка ответа, нормализация), веб-слой (авторизация, CSRF, права, все
страницы на «грязных» данных), фоновый воркер.

## Структура

```
app.py                  маршруты Flask, авторизация, webhook
database.py             PostgreSQL/SQLite, миграции, очередь, дедупликация Zoom
security.py             SECRET_KEY, CSRF, лимит попыток входа, безопасные редиректы
services/
  pipeline.py           конвейер: приём Zoom-встреч, обработка записи, повторы
  background.py         фоновый воркер: лидер (flock), очередь, heartbeat, поллер
  zoom.py               Zoom API: OAuth (кэш), записи, скачивание, подпись webhook
  poller.py             резервная проверка Zoom
  transcript_text.py    разбор VTT/TXT, статистика спикеров
  transcription.py      Whisper + ffmpeg (сжатие и нарезка)
  llm.py                клиент Claude, structured outputs, классификация ошибок
  analysis.py           промпты и схемы чек-листов, нормализация ответа
  detection.py          тип записи и имя тренера/менеджера
  insights.py           аналитика по всей базе
  health.py             проверки интеграций (страница «Система»)
  timeutil.py           UTC ↔ Europe/Kyiv
templates/, static/     интерфейс (украинский)
tests/                  pytest
create_admin.py         создать админа / сбросить пароль
sync_zoom.py            ручная синхронизация Zoom из консоли
```
