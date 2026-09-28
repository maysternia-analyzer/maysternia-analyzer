"""
Журнал подій застосунку.

• Усі логи (INFO+) — у stdout з контекстом (`rid=… user=… record=…`): їх показує Railway → Logs.
• Помилки, попередження і дії користувачів (логер «audit») — ще й у таблицю app_logs:
  сторінка «Журнал» в адмінці. Однакові помилки групуються (лічильник замість тисячі рядків).
• Секрети (токени, ключі, паролі в URL) маскуються і в stdout, і в БД.
• Запис у БД іде у фоновому потоці пачками: логування ніколи не гальмує і не ламає запит.
"""
import contextvars
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from collections import deque
from contextlib import contextmanager

AUDIT = "audit"            # логер дій користувачів — теж потрапляє в журнал
CLIENT = "browser"         # помилки JavaScript з браузера
GROUP_WINDOW_MINUTES = 60  # повтор тієї ж помилки протягом години — лише +1 до лічильника
MAX_MESSAGE = 2000
MAX_DETAILS = 20000

_context: contextvars.ContextVar[dict] = contextvars.ContextVar("log_context", default={})

_SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), r"\1 ***"),
    (re.compile(r"(?i)\b(access_token|refresh_token|token|api_key|apikey|password|passwd|secret|signature|"
                r"x-zm-signature)(\"?\s*[=:]\s*\"?)([^&\s\"',;]+)"), r"\1\2***"),
    (re.compile(r"sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{12,}"), "sk-***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "gh_***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github_pat_***"),
    (re.compile(r"(postgres(?:ql)?://[^:/\s@]+):[^@\s]+@"), r"\1:***@"),
    (re.compile(r"\bbot\d{5,}:[A-Za-z0-9_\-]{20,}"), "bot***"),
]


def redact(text) -> str:
    """Маскує токени, ключі та паролі в тексті логу."""
    text = str(text or "")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ── Контекст (запит / задача) ─────────────────────────────────────────────────

def bind(**values) -> contextvars.Token:
    """Додає поля до контексту поточного потоку. Повертає токен для reset()."""
    merged = {**_context.get(), **{k: v for k, v in values.items() if v not in (None, "")}}
    return _context.set(merged)


def reset(token: contextvars.Token) -> None:
    try:
        _context.reset(token)
    except ValueError:  # токен з іншого контексту — просто очищаємо
        _context.set({})


@contextmanager
def context(**values):
    token = bind(**values)
    try:
        yield
    finally:
        reset(token)


def current_context() -> dict:
    return dict(_context.get())


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _context.get()
        record.ctx = dict(ctx)
        record.ctx_suffix = (" | " + " ".join(f"{k}={v}" for k, v in ctx.items())) if ctx else ""
        return True


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "ctx_suffix"):
            record.ctx_suffix = ""
        return redact(super().format(record))


# ── Запис у БД ────────────────────────────────────────────────────────────────

_local = threading.local()


def fingerprint(level: str, logger: str, template: str, exc_info=None) -> str:
    """Ключ групування: місце в коді + шаблон повідомлення (без конкретних id/чисел)."""
    parts = [level, logger, re.sub(r"\d+", "#", str(template))[:300]]
    if exc_info and exc_info[0]:
        parts.append(exc_info[0].__name__)
        frames = traceback.extract_tb(exc_info[2]) if exc_info[2] else []
        if frames:
            parts.append(f"{os.path.basename(frames[-1].filename)}:{frames[-1].lineno}")
    return hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()


def entry_from_record(record: logging.LogRecord) -> dict:
    try:
        message = record.getMessage()
    except Exception:  # некоректні аргументи форматування не повинні губити подію
        message = str(record.msg)
    details = ""
    if record.exc_info:
        details = "".join(traceback.format_exception(*record.exc_info))
    elif record.stack_info:
        details = record.stack_info
    extra_details = getattr(record, "details", None)
    if extra_details:
        details = (str(extra_details) + ("\n\n" + details if details else ""))
    ctx = getattr(record, "ctx", None) or dict(_context.get())
    return {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(record.created)),
        "level": record.levelname,
        "logger": record.name[:100],
        "message": redact(message)[:MAX_MESSAGE],
        "details": redact(details)[-MAX_DETAILS:],
        "context": json.dumps({k: str(v)[:300] for k, v in ctx.items()}, ensure_ascii=False),
        "fingerprint": fingerprint(record.levelname, record.name, record.msg, record.exc_info),
    }


class DbLogHandler(logging.Handler):
    """Складає WARNING+ і дії користувачів у чергу; фоновий потік пише їх у БД пачками."""

    def __init__(self, capacity: int = 1000):
        super().__init__(level=logging.INFO)
        self.queue: deque = deque(maxlen=capacity)
        self.dropped = 0
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._pid = os.getpid()
        self.sync = False  # тести: писати одразу, без фонового потоку
        self.on_new_error = None  # колбек для Telegram-сповіщень про нові помилки

    def accepts(self, record: logging.LogRecord) -> bool:
        if getattr(_local, "writing", False):  # логи самого запису в БД — не зациклюємося
            return False
        return record.levelno >= logging.WARNING or record.name in (AUDIT, CLIENT)

    def emit(self, record: logging.LogRecord) -> None:
        if not self.accepts(record):
            return
        try:
            entry = entry_from_record(record)
        except Exception:
            return
        if self.sync:
            self._write([entry])
            return
        if len(self.queue) == self.queue.maxlen:
            self.dropped += 1
        self.queue.append(entry)
        self._ensure_thread()
        self._wake.set()

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive() and self._pid == os.getpid():
            return
        with self._thread_lock:
            if self._thread and self._thread.is_alive() and self._pid == os.getpid():
                return
            self._pid = os.getpid()  # після fork потік батьківського процесу не існує
            self._thread = threading.Thread(target=self._run, name="applog-writer", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            self._wake.wait(timeout=5)
            self._wake.clear()
            time.sleep(0.5)  # збираємо пачку
            self.flush_queue()

    def flush_queue(self) -> None:
        batch = []
        while self.queue:
            try:
                batch.append(self.queue.popleft())
            except IndexError:
                break
        if self.dropped:
            batch.append({
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "level": "WARNING",
                "logger": "applog", "message": f"Журнал переповнений: пропущено {self.dropped} подій",
                "details": "", "context": "{}", "fingerprint": fingerprint("WARNING", "applog", "overflow"),
            })
            self.dropped = 0
        if batch:
            self._write(batch)

    def _write(self, batch: list[dict]) -> None:
        import database as db  # тут, бо database імпортує логування раніше за цей модуль
        _local.writing = True
        try:
            for entry in batch:
                try:
                    is_new = db.log_event(entry, GROUP_WINDOW_MINUTES)
                except Exception as e:  # БД недоступна — подія лишається лише в stdout
                    print(f"applog: не вдалося записати подію в БД: {e}", file=sys.stderr)
                    return
                if is_new and entry["level"] in ("ERROR", "CRITICAL") and self.on_new_error:
                    try:
                        self.on_new_error(entry)
                    except Exception as e:
                        print(f"applog: сповіщення не надіслано: {e}", file=sys.stderr)
        finally:
            _local.writing = False


_db_handler: DbLogHandler | None = None


def db_handler() -> DbLogHandler | None:
    return _db_handler


def setup(level: str | None = None, db_enabled: bool = True) -> None:
    """Налаштовує кореневий логер. Ідемпотентно (повторний виклик не дублює обробники)."""
    global _db_handler
    root = logging.getLogger()
    root.setLevel((level or os.environ.get("LOG_LEVEL", "INFO")).upper())
    for handler in list(root.handlers):
        if getattr(handler, "_maysternia", False):
            root.removeHandler(handler)
    context_filter = _ContextFilter()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(_RedactingFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s%(ctx_suffix)s"))
    stream.addFilter(context_filter)
    stream._maysternia = True
    root.addHandler(stream)
    if db_enabled:
        _db_handler = DbLogHandler()
        _db_handler.addFilter(context_filter)
        _db_handler._maysternia = True
        root.addHandler(_db_handler)
    # Дії користувачів і помилки браузера пишемо завжди, навіть якщо LOG_LEVEL=WARNING.
    logging.getLogger(AUDIT).setLevel(logging.INFO)
    logging.getLogger(CLIENT).setLevel(logging.INFO)
    # Бібліотеки, що шумлять на INFO (кожен HTTP-запит), — лише попередження.
    for noisy in ("httpx", "httpcore", "urllib3", "werkzeug"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def flush() -> None:
    """Записує все з черги в БД (для тестів і перед завершенням процесу)."""
    if _db_handler:
        _db_handler.flush_queue()


# ── Збої сервера (gunicorn.conf.py) ───────────────────────────────────────────

def log_thread_dump(message: str) -> None:
    """Критична подія зі стеками всіх потоків процесу (для завислих запитів)."""
    frames = sys._current_frames()
    names = {t.ident: t.name for t in threading.enumerate()}
    dump = "\n\n".join(f"Потік {names.get(ident, ident)}:\n" + "".join(traceback.format_stack(frame))
                       for ident, frame in frames.items())
    logging.getLogger("server").critical(message, extra={"details": dump})


def ingest_server_errors(path: str) -> int:
    """Переносить рядки, записані майстром gunicorn (загибель воркерів, OOM), у журнал. Один воркер — один раз."""
    claimed = f"{path}.{os.getpid()}"
    try:
        os.replace(path, claimed)  # атомарно: другий воркер файлу вже не побачить
    except FileNotFoundError:
        return 0
    server_log = logging.getLogger("server")
    count = 0
    try:
        with open(claimed, encoding="utf-8", errors="replace") as f:
            for line in f.read().splitlines()[-200:]:
                if line.strip():
                    # Повідомлення без аргументів: різні збої — різні групи (pid замінюється на # у ключі).
                    server_log.error(("Gunicorn: " + line.strip())[:MAX_MESSAGE])
                    count += 1
    finally:
        try:
            os.remove(claimed)
        except OSError:
            pass
    return count
