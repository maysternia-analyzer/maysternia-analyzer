"""
Хуки gunicorn (файл підхоплюється автоматично з робочої директорії; параметри запуску — у railway.toml).

Мета — щоб у «Журналі» були й збої самого сервера, а не лише помилки застосунку:
• воркер завис і вбитий за таймаутом → стеки всіх потоків (де саме завис запит);
• воркер загинув (OOM-kill, падіння) → майстер пише рядок у файл, новий воркер переносить його в журнал.
Майстер не імпортує застосунок і не підключається до БД (безпечно для fork).
"""
import logging
import os

GUNICORN_ERRORS_FILE = os.environ.get("GUNICORN_ERRORS_FILE", "/tmp/maysternia-gunicorn-errors.log")


def on_starting(server):
    """Майстер: помилки gunicorn (загибель воркерів, таймаути) — ще й у файл для журналу."""
    handler = logging.FileHandler(GUNICORN_ERRORS_FILE, encoding="utf-8", delay=True)
    handler.setLevel(logging.ERROR)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("gunicorn.error").addHandler(handler)


def post_worker_init(worker):
    """Воркер після завантаження застосунку: переносимо збої попередніх воркерів у журнал."""
    try:
        from services import applog
        applog.ingest_server_errors(GUNICORN_ERRORS_FILE)
    except Exception as e:  # журнал не повинен заважати старту
        logging.getLogger("gunicorn.error").warning("Не вдалося перенести збої gunicorn у журнал: %s", e)


def worker_abort(worker):
    """Воркер отримав SIGABRT (таймаут запиту): зберігаємо стеки потоків, щоб знайти, де завис."""
    try:
        from services import applog
        applog.log_thread_dump(f"Воркер pid={worker.pid} перервано через таймаут — запит завис")
        applog.flush()
    except Exception:
        pass
