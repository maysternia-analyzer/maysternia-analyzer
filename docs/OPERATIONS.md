# Деплой и эксплуатация

## Деплой (Railway)

Railway собирает проект автоматически при пуше в ветку `main` репозитория
`maysternia-analyzer/maysternia-analyzer` (nixpacks, ~2 минуты).

> **На 27.09.2026 автодеплой не срабатывает**: последний деплой в GitHub — 26.06.2026, прод
> работает на старой версии (признак — `/healthz` отвечает 404). Запустить вручную:
> Railway → проект `gallant-balance` → сервис `web` → Deployments → **Deploy latest commit**;
> чтобы починить автодеплой — Settings → Source → переподключить GitHub-репозиторий и
> проверить, что выбрана ветка `main`. После деплоя `/healthz` должен отвечать `{"ok": true, "db": true}`.

Конфигурация — `railway.toml`:

```
gunicorn app:app --workers 2 --threads 4 --worker-class gthread --timeout 600 --no-control-socket
healthcheckPath = /healthz   (проверяет доступность БД)
```

Пока новая версия не прошла healthcheck, работает старая. При переключении незавершённые
задачи старой версии автоматически возвращаются в очередь (через ~5 минут).

ffmpeg на Railway ставит nixpacks: он добавляет пакет `ffmpeg-headless`, **потому что в
`requirements.txt` есть `pydub`** (в коде не используется — не удалять!). Проверить — страница «Система».
Перед первым деплоем новой версии: записи в статусах `processing`/`analyzing`, зависшие со
старой версии, будут автоматически обработаны заново (это вызовы Claude).

## Переменные окружения (Railway → Variables)

| Переменная | Обязательна | Назначение |
|---|---|---|
| `DATABASE_URL` | да (ставится Railway) | PostgreSQL |
| `ANTHROPIC_API_KEY` | да | Claude |
| `SECRET_KEY` | рекомендуется | Ключ сессий; **должен быть уникальным** (≥16 символов) |
| `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET` | для Zoom | Server-to-Server OAuth |
| `ZOOM_WEBHOOK_SECRET` | для Zoom | Secret Token приложения в Zoom Marketplace |
| `OPENAI_API_KEY` | нет | Whisper для аудио без транскрипции Zoom |
| `PUBLIC_BASE_URL` | нет | Адрес сайта для ссылок в Telegram/вебхуке (на Railway берётся из `RAILWAY_PUBLIC_DOMAIN`) |
| `ANTHROPIC_MODEL` | нет | По умолчанию `claude-sonnet-4-6`. Новые модели (Sonnet 5, Opus 5+) по умолчанию «думают» — это дороже и тратит `max_tokens` |
| `APP_TIMEZONE` | нет | По умолчанию `Europe/Kyiv` |
| `ZOOM_POLL_INTERVAL_MINUTES` / `ZOOM_POLL_LOOKBACK_DAYS` | нет | 5 / 3 |
| `ZOOM_MIN_DURATION_MINUTES` | нет | Короче — не анализировать (3) |
| `ZOOM_TRANSCRIPT_WAIT_MINUTES` | нет | Сколько ждать VTT от Zoom перед Whisper (180) |
| `ZOOM_IGNORE_SPEAKERS` | нет | Имена аккаунтов-организаторов через запятую («Код Харизми») |
| `JOB_CONCURRENCY` / `JOB_MAX_ATTEMPTS` | нет | 2 / 3 |
| `DB_POOL_MIN` / `DB_POOL_MAX` | нет | Соединений PostgreSQL на процесс: держать открытыми / максимум (3 / 8) |

Большинство параметров (модель Claude, пороги, Telegram, вебхук, параметры Zoom, описание компании,
чек-листы) настраиваются в интерфейсе: **Налаштування** и **Чек-листи** (только администратор).
Значения из переменных окружения служат значениями по умолчанию.

## Zoom

- Приложение Server-to-Server OAuth в аккаунте «Код Харизми». Нужные scopes:
  `cloud_recording:read:list_user_recordings:admin`, `cloud_recording:read:recording:admin`,
  `cloud_recording:read:list_recording_files:admin` (уже выданы).
- Event Subscriptions: `recording.completed`, `recording.transcript_completed`,
  URL `https://web-production-420d0.up.railway.app/zoom/webhook`.
- Чтобы не нужен был Whisper, в Zoom включите: Settings → Recording → Cloud recording →
  **Create audio transcript**.
- Приложение видит записи пользователя-владельца (`users/me`). Если встречи записывают
  другие пользователи аккаунта, их записи не попадут в систему (нужен scope
  `cloud_recording:read:list_account_recordings:admin` и доработка поллера).

## Типовые задачи

| Задача | Как |
|---|---|
| Проверить ключи Zoom / Claude / OpenAI | «Система» → «Перевірити інтеграції» |
| Подтянуть записи за N дней (если вебхук не дошёл) | «Система» → «Перевірити Zoom зараз» (1–180 дней) |
| Журнал вебхуков | «Система» или `/admin/webhook-logs` (JSON) |
| Запись в ошибке | Открыть запись → кнопка повтора (анализ / загрузка из Zoom) |
| Неверно определён тип/имя | «Редагувати» на странице записи (смена типа сама перезапустит анализ) |
| Создать админа / сбросить пароль | `python create_admin.py` (с `DATABASE_URL` прод-БД) |
| Синхронизация из консоли | `python sync_zoom.py 14 [--process]` |

## Диагностика

- **Запись долго «В черзі»** с текстом «Очікуємо транскрипцію від Zoom» — Zoom ещё не создал
  VTT; система проверяет каждые 15 минут до 3 часов, затем пробует Whisper.
- **«Ключ OpenAI недійсний»** — обновить `OPENAI_API_KEY` или загружать VTT/TXT.
- **«Файл запису не знайдено на сервері»** — диск Railway очищается при деплое; для ручных
  загрузок аудио загрузите файл снова (транскрипции VTT/TXT хранятся в БД и не теряются).
- **Вебхуки `error_bad_signature`** — `ZOOM_WEBHOOK_SECRET` не совпадает с Secret Token в Zoom
  (пишется не чаще раза в минуту). Подпись проверяется и для `endpoint.url_validation`.
- Логи — Railway → Deployments → Logs (формат `время уровень [модуль] сообщение`).
