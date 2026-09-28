"""
Шар роботи з базою даних.

- Продакшн (Railway): PostgreSQL, якщо задано DATABASE_URL.
- Локально / тести: SQLite (шлях з SQLITE_PATH або data.db поруч з кодом).

SQL у модулі пишеться з плейсхолдерами `?`; для PostgreSQL вони автоматично
перетворюються на `%s` (а літеральні `%` екрануються).
"""
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = Path(os.environ.get("SQLITE_PATH") or Path(__file__).parent / "data.db")

if USE_POSTGRES:
    import psycopg2
    import psycopg2.pool

RECORD_TYPES = ("lesson", "sales")
# queued → processing (завантаження/транскрипція) → analyzing → done | error
ACTIVE_STATUSES = ("queued", "processing", "analyzing")

# Колонки таблиці records: (назва, тип SQLite, тип PostgreSQL).
# Нові колонки додаються автоматично при старті (див. _migrate_records).
RECORD_COLUMNS = [
    ("created_at", "TEXT NOT NULL DEFAULT ''", "TEXT NOT NULL DEFAULT ''"),
    ("updated_at", "TEXT", "TEXT"),
    ("record_date", "TEXT NOT NULL DEFAULT ''", "TEXT NOT NULL DEFAULT ''"),
    ("record_time", "TEXT DEFAULT ''", "TEXT DEFAULT ''"),
    ("record_type", "TEXT NOT NULL DEFAULT 'sales'", "TEXT NOT NULL DEFAULT 'sales'"),
    ("person_name", "TEXT NOT NULL DEFAULT ''", "TEXT NOT NULL DEFAULT ''"),
    ("trainer_name", "TEXT DEFAULT ''", "TEXT DEFAULT ''"),
    ("filename", "TEXT", "TEXT"),
    ("transcription", "TEXT", "TEXT"),
    ("analysis_json", "TEXT", "TEXT"),
    ("manager_comment", "TEXT", "TEXT"),
    ("status", "TEXT DEFAULT 'queued'", "TEXT DEFAULT 'queued'"),
    ("sale_made", "INTEGER DEFAULT NULL", "INTEGER DEFAULT NULL"),
    ("sale_amount", "REAL DEFAULT NULL", "REAL DEFAULT NULL"),
    # --- колонки пайплайна обробки ---
    ("error_message", "TEXT", "TEXT"),
    ("source", "TEXT DEFAULT ''", "TEXT DEFAULT ''"),          # upload | zoom | text
    ("source_json", "TEXT", "TEXT"),                          # метадані Zoom-запису
    ("zoom_meeting_uuid", "TEXT", "TEXT"),
    ("job_kind", "TEXT DEFAULT 'full'", "TEXT DEFAULT 'full'"),  # full | analyze
    ("auto_detect", "INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),  # 1 = тип/імʼя визначає AI
    ("attempts", "INTEGER DEFAULT 0", "INTEGER DEFAULT 0"),
    ("locked_at", "TEXT", "TEXT"),                            # heartbeat активної обробки
    ("not_before", "TEXT", "TEXT"),                           # відкладений старт (UTC ISO)
    # --- «легкі» поля з аналізу: списки й статистика без розбору analysis_json ---
    ("analysis_kind", "TEXT", "TEXT"),
    ("score", "INTEGER", "INTEGER"),
    ("checklist_done", "INTEGER", "INTEGER"),
    ("checklist_total", "INTEGER", "INTEGER"),
    ("summary", "TEXT", "TEXT"),
    ("deal_chance", "TEXT", "TEXT"),
    ("deal_chance_percent", "INTEGER", "INTEGER"),
    ("lead_temperature", "TEXT", "TEXT"),
    ("engagement_level", "TEXT", "TEXT"),
    ("analyzed_at", "TEXT", "TEXT"),
]
LIGHT_FIELDS = ("analysis_kind", "score", "checklist_done", "checklist_total", "summary", "deal_chance",
                "deal_chance_percent", "lead_temperature", "engagement_level")
_RECORD_COLUMN_NAMES = {c[0] for c in RECORD_COLUMNS}
ROLES = ("admin", "viewer", "manager")
# Колонки users, що зʼявились після першої версії: (назва, тип SQLite, тип PostgreSQL).
USER_EXTRA_COLUMNS = [("person_name", "TEXT DEFAULT ''", "TEXT DEFAULT ''")]

# Колонки для списків (без важкої транскрипції).
_LIST_COLUMNS = ", ".join(
    ["id"] + [c for c, _, _ in RECORD_COLUMNS if c not in ("transcription", "source_json")]
)
# Ще легші — без analysis_json (дашборд, списки, статистика).
_LIGHT_COLUMNS = ", ".join(
    ["id"] + [c for c, _, _ in RECORD_COLUMNS if c not in ("transcription", "source_json", "analysis_json")]
)


# ── Час ──────────────────────────────────────────────────────────────────────

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_iso(delta: timedelta | None = None) -> str:
    dt = utcnow() + (delta or timedelta())
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ── Підключення ───────────────────────────────────────────────────────────────

_pool = None
_pool_pid = None
_pool_lock = threading.Lock()
_pool_sem = None
_last_used: dict[int, float] = {}
_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "8"))
# psycopg2 тримає в пулі не більше minconn простоюючих зʼєднань (решта закривається).
_POOL_MIN = min(_POOL_MAX, int(os.environ.get("DB_POOL_MIN", "3")))


class DatabaseBusyError(RuntimeError):
    """Усі зʼєднання зайняті — тимчасова ситуація, варто повторити пізніше."""
    transient = True


def _get_pool():
    """Пул створюється ліниво в кожному процесі (після fork у gunicorn)."""
    global _pool, _pool_pid, _pool_sem
    pid = os.getpid()
    if _pool is None or _pool_pid != pid:
        with _pool_lock:
            if _pool is None or _pool_pid != pid:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    _POOL_MIN, _POOL_MAX, DATABASE_URL, connect_timeout=10
                )
                _pool_sem = threading.BoundedSemaphore(_POOL_MAX)
                _pool_pid = pid
                _last_used.clear()
    return _pool


def _pg_checkout(pool):
    """Бере зʼєднання з пулу; ті, що довго простоювали, перевіряє (Postgres міг перезапуститись)."""
    for _ in range(_POOL_MAX + 1):
        conn = pool.getconn()
        last = _last_used.get(id(conn))
        if not conn.closed and last is not None and time.monotonic() - last <= 30:
            return conn
        try:
            if conn.closed:
                raise psycopg2.InterfaceError("closed")
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()
            return conn
        except Exception:
            _last_used.pop(id(conn), None)
            pool.putconn(conn, close=True)
    raise psycopg2.OperationalError("Не вдалося отримати робоче зʼєднання з БД")


@contextmanager
def connect():
    """Зʼєднання з БД; commit при успіху, rollback при помилці."""
    if USE_POSTGRES:
        pool = _get_pool()
        sem = _pool_sem
        if not sem.acquire(timeout=30):
            raise DatabaseBusyError("Немає вільних зʼєднань з БД (пул вичерпано)")
        conn = None
        broken = False
        try:
            conn = _pg_checkout(pool)
            yield conn
            conn.commit()
        except Exception as e:
            broken = isinstance(e, (psycopg2.OperationalError, psycopg2.InterfaceError))
            if conn is not None and not conn.closed:
                try:
                    conn.rollback()
                except Exception:
                    broken = True
            raise
        finally:
            if conn is not None:
                _last_used[id(conn)] = time.monotonic()
                pool.putconn(conn, close=broken or bool(conn.closed))
            sem.release()
    else:
        conn = sqlite3.connect(SQLITE_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")  # безпечно в режимі WAL, значно швидше
        # Вбудований LOWER у SQLite розуміє лише латиницю — підміняємо на Unicode-версію.
        conn.create_function("lower", 1, lambda v: v.lower() if isinstance(v, str) else v, deterministic=True)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _sql(query: str) -> str:
    if USE_POSTGRES:
        return query.replace("%", "%%").replace("?", "%s")
    return query


def _rows_to_dicts(cur, rows):
    if not rows:
        return []
    if USE_POSTGRES:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]
    return [dict(r) for r in rows]


def fetch_all(query: str, params=()) -> list[dict]:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(_sql(query), tuple(params))
        rows = _rows_to_dicts(cur, cur.fetchall())
        cur.close()
    return rows


def fetch_one(query: str, params=()) -> dict | None:
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(_sql(query), tuple(params))
        row = cur.fetchone()
        result = _rows_to_dicts(cur, [row])[0] if row is not None else None
        cur.close()
    return result


def execute(query: str, params=()) -> int:
    """Виконує запит і повертає кількість змінених рядків."""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(_sql(query), tuple(params))
        count = cur.rowcount
        cur.close()
    return count


def insert(query: str, params=()) -> int:
    """INSERT, що повертає id нового рядка."""
    with connect() as conn:
        cur = conn.cursor()
        if USE_POSTGRES:
            cur.execute(_sql(query) + " RETURNING id", tuple(params))
            new_id = cur.fetchone()[0]
        else:
            cur.execute(query, tuple(params))
            new_id = cur.lastrowid
        cur.close()
    return new_id


# ── Схема та міграції ─────────────────────────────────────────────────────────

def _pk() -> str:
    return "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"


def _schema_statements() -> list[str]:
    col_idx = 2 if USE_POSTGRES else 1
    record_cols = ",\n".join(f"{c[0]} {c[col_idx]}" for c in RECORD_COLUMNS)
    return [
        f"CREATE TABLE IF NOT EXISTS records (id {_pk()},\n{record_cols})",
        f"""CREATE TABLE IF NOT EXISTS users (
                id {_pk()},
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                person_name TEXT DEFAULT '')""",
        f"""CREATE TABLE IF NOT EXISTS insights_cache (
                id {_pk()},
                updated_at TEXT NOT NULL,
                date_from TEXT DEFAULT '',
                date_to TEXT DEFAULT '',
                data_json TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS zoom_processed (
                zoom_file_id TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS webhook_log (
                id {_pk()},
                received_at TEXT NOT NULL,
                event TEXT,
                status TEXT,
                details TEXT)""",
        """CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS app_logs (
                id {_pk()},
                created_at TEXT NOT NULL,
                last_at TEXT NOT NULL,
                level TEXT NOT NULL,
                logger TEXT,
                message TEXT,
                details TEXT,
                context TEXT,
                fingerprint TEXT,
                count INTEGER NOT NULL DEFAULT 1)""",
    ]


def _existing_columns(cur, table: str) -> set[str]:
    if USE_POSTGRES:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}
    cur.execute(f"PRAGMA table_info({table})")
    return {r[1] for r in cur.fetchall()}


def _migrate_table(cur, table: str, columns: list[tuple[str, str, str]]):
    existing = _existing_columns(cur, table)
    for name, sqlite_type, pg_type in columns:
        if name in existing:
            continue
        definition = (pg_type if USE_POSTGRES else sqlite_type).replace("NOT NULL ", "")
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise
            continue
        log.info("Міграція: додано колонку %s.%s", table, name)


def _migrate_records(cur):
    existing = _existing_columns(cur, "records")
    for name, sqlite_type, pg_type in RECORD_COLUMNS:
        if name in existing:
            continue
        # Для старих таблиць додаємо колонки без NOT NULL — там уже є дані.
        definition = (pg_type if USE_POSTGRES else sqlite_type).replace("NOT NULL ", "")
        try:
            cur.execute(f"ALTER TABLE records ADD COLUMN {name} {definition}")
        except sqlite3.OperationalError as e:  # інший процес SQLite встиг додати колонку
            if "duplicate column" not in str(e):
                raise
            continue
        log.info("Міграція: додано колонку records.%s", name)
        if name == "job_kind":
            # Старі записи з готовою транскрипцією при повторі лише переаналізовуємо.
            cur.execute(_sql(
                "UPDATE records SET job_kind = 'analyze' WHERE transcription IS NOT NULL "
                "AND transcription != '' AND transcription NOT LIKE '[ПОМИЛКА%'"
            ))


def init_db():
    """Створює таблиці, додає відсутні колонки та індекси. Ідемпотентно."""
    with connect() as conn:
        cur = conn.cursor()
        if USE_POSTGRES:
            # Кілька воркерів gunicorn стартують одночасно — серіалізуємо DDL.
            cur.execute("SELECT pg_advisory_xact_lock(727274001)")
            # Не блокуємо всю таблицю надовго, якщо хтось тримає довгу транзакцію.
            cur.execute("SET LOCAL lock_timeout = '15s'")
        else:
            cur.execute("PRAGMA journal_mode=WAL")
        for stmt in _schema_statements():
            cur.execute(stmt)
        _migrate_records(cur)
        _migrate_table(cur, "users", USER_EXTRA_COLUMNS)
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_records_status ON records(status)",
            "CREATE INDEX IF NOT EXISTS idx_records_date ON records(record_date)",
            "CREATE INDEX IF NOT EXISTS idx_records_meeting ON records(zoom_meeting_uuid)",
            "CREATE INDEX IF NOT EXISTS idx_app_logs_last ON app_logs(last_at)",
            "CREATE INDEX IF NOT EXISTS idx_app_logs_fingerprint ON app_logs(fingerprint)",
        ):
            cur.execute(idx_sql)
        cur.close()


# ── Налаштування застосунку ───────────────────────────────────────────────────

def get_setting(key: str) -> str | None:
    row = fetch_one("SELECT value FROM app_settings WHERE key = ?", (key,))
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def set_setting_if_absent(key: str, value: str) -> str:
    """Атомарно зберігає значення, якщо його ще немає; повертає актуальне."""
    execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
        (key, value),
    )
    return get_setting(key)


# ── Записи (records) ──────────────────────────────────────────────────────────

def analysis_kind(analysis) -> str | None:
    """Визначає, для якого типу запису зроблено аналіз (lesson / sales)."""
    if not isinstance(analysis, dict):
        return None
    kind = analysis.get("_kind")
    if kind in RECORD_TYPES:
        return kind
    if "checklist_score" in analysis or "deal_chance" in analysis:
        return "sales"
    if "overall_score" in analysis:
        return "lesson"
    return None


_SCORE_KEYS = ("overall_score", "checklist_score", "deal_chance_percent")
_CRITERIA_KEYS = ("greeting", "safe_atmosphere", "structure_explained", "practical_exercises",
                  "feedback_received", "transition_to_manager", "need_identified", "presentation_done",
                  "objections_handled", "urgency_used", "next_step_offered")


def _coerce_analysis(analysis: dict) -> dict:
    """Старі записи містять «сирий» JSON моделі: рядкові бали, не-словники тощо."""
    for key in _SCORE_KEYS:
        if key in analysis:
            try:
                analysis[key] = max(0, min(100, int(round(float(analysis[key])))))
            except (TypeError, ValueError):
                analysis[key] = 0
    for key in _CRITERIA_KEYS:
        if key in analysis and not isinstance(analysis[key], dict):
            analysis[key] = {"result": bool(analysis[key]), "comment": ""}
    mistakes = analysis.get("top_mistakes")
    if mistakes is not None and not isinstance(mistakes, list):
        analysis["top_mistakes"] = [str(mistakes)] if mistakes else []
    return analysis


def _decorate(row: dict | None) -> dict | None:
    """Розбирає analysis_json і прибирає аналіз, що не відповідає типу запису."""
    if row is None:
        return None
    analysis = None
    raw = row.get("analysis_json")
    if raw:
        try:
            analysis = json.loads(raw)
        except (TypeError, ValueError):
            log.warning("Пошкоджений analysis_json у записі %s", row.get("id"))
    if analysis is not None and analysis_kind(analysis) != row.get("record_type"):
        row["analysis_stale"] = True
        analysis = None
    row["analysis"] = _coerce_analysis(analysis) if isinstance(analysis, dict) else None
    return row


def _record_insert(record_date, record_type, person_name, filename="", *, record_time="",
                   trainer_name="", status="queued", source="upload", source_json=None,
                   zoom_meeting_uuid=None, job_kind="full", auto_detect=False,
                   not_before=None, transcription=None) -> tuple[str, tuple]:
    now = utcnow_iso()
    sql = """INSERT INTO records (created_at, updated_at, record_date, record_time, record_type,
               person_name, trainer_name, filename, status, source, source_json,
               zoom_meeting_uuid, job_kind, auto_detect, attempts, not_before, transcription)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)"""
    params = (now, now, record_date, record_time or "", record_type, person_name or "",
              trainer_name or "", filename or "", status, source,
              json.dumps(source_json, ensure_ascii=False) if source_json is not None else None,
              zoom_meeting_uuid, job_kind, 1 if auto_detect else 0, not_before, transcription)
    return sql, params


def create_record(record_date, record_type, person_name, filename="", **fields) -> int:
    sql, params = _record_insert(record_date, record_type, person_name, filename, **fields)
    return insert(sql, params)


def create_zoom_record_once(claim_keys: list[str], record_date, record_type, person_name,
                            filename, **fields) -> int | None:
    """
    В одній транзакції «забирає» ключі дедуплікації (перший — ключ зустрічі) і створює запис.
    Якщо зустріч уже забрана — None. Збій посередині відкочує все, тож зустріч не «губиться».
    """
    now = utcnow_iso()
    sql, params = _record_insert(record_date, record_type, person_name, filename, **fields)
    claim_sql = _sql("INSERT INTO zoom_processed (zoom_file_id, processed_at) VALUES (?, ?) "
                     "ON CONFLICT (zoom_file_id) DO NOTHING")
    with connect() as conn:
        cur = conn.cursor()
        for i, key in enumerate(claim_keys):
            cur.execute(claim_sql, (key, now))
            if i == 0 and cur.rowcount != 1:
                cur.close()
                return None
        if USE_POSTGRES:
            cur.execute(_sql(sql) + " RETURNING id", params)
            record_id = cur.fetchone()[0]
        else:
            cur.execute(sql, params)
            record_id = cur.lastrowid
        cur.close()
    return record_id


def light_fields_from_json(raw) -> dict:
    """«Легкі» поля з analysis_json (оцінка, чек-ліст X/Y, резюме…)."""
    empty = dict.fromkeys(LIGHT_FIELDS)
    try:
        analysis = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return empty
    kind = analysis_kind(analysis)
    if not kind:
        return empty
    from services.analysis import light_fields  # лінивий імпорт: services імпортують database
    return light_fields(_coerce_analysis(analysis), kind)


def update_record(record_id: int, **fields) -> int:
    """Оновлює довільні колонки запису. analysis / source_json серіалізуються в JSON."""
    if "analysis" in fields:
        analysis = fields.pop("analysis")
        fields["analysis_json"] = (
            json.dumps(analysis, ensure_ascii=False) if analysis is not None else None
        )
    if "analysis_json" in fields:
        fields.update(light_fields_from_json(fields["analysis_json"]))
        if fields["analysis_json"] and not fields.get("analyzed_at"):
            fields["analyzed_at"] = utcnow_iso()
    if "source_json" in fields and not isinstance(fields["source_json"], (str, type(None))):
        fields["source_json"] = json.dumps(fields["source_json"], ensure_ascii=False)
    unknown = set(fields) - _RECORD_COLUMN_NAMES
    if unknown:
        raise ValueError(f"Невідомі колонки records: {sorted(unknown)}")
    fields["updated_at"] = utcnow_iso()
    assignments = ", ".join(f"{name} = ?" for name in fields)
    return execute(
        f"UPDATE records SET {assignments} WHERE id = ?", (*fields.values(), record_id)
    )


def get_record(record_id: int) -> dict | None:
    return _decorate(fetch_one("SELECT * FROM records WHERE id = ?", (record_id,)))


def get_all_records(record_type=None, person_name=None, date_from=None, date_to=None,
                    trainer_name=None, transcript_preview_chars: int = 0) -> list[dict]:
    """Список записів без повної транскрипції (для дашбордів)."""
    columns = _LIST_COLUMNS
    if transcript_preview_chars:
        columns += f", SUBSTR(transcription, 1, {int(transcript_preview_chars)}) AS transcription"
    where, params = _filters_sql(record_type, person_name, trainer_name, date_from, date_to)
    query = f"SELECT {columns} FROM records WHERE 1=1{where} ORDER BY record_date DESC, record_time DESC, id DESC"
    return [_decorate(r) for r in fetch_all(query, params)]


def _filters_sql(record_type=None, person_name=None, trainer_name=None, date_from=None, date_to=None):
    query, params = "", []
    for column, op, value in (
        ("record_type", "=", record_type),
        ("person_name", "=", person_name),
        ("trainer_name", "=", trainer_name),
        ("record_date", ">=", date_from),
        ("record_date", "<=", date_to),
    ):
        if value:
            query += f" AND {column} {op} ?"
            params.append(value)
    return query, params


def list_records(record_type=None, person_name=None, trainer_name=None, date_from=None, date_to=None) -> list[dict]:
    """Легкі рядки для списків і статистики: без транскрипції та JSON аналізу."""
    where, params = _filters_sql(record_type, person_name, trainer_name, date_from, date_to)
    rows = fetch_all(f"SELECT {_LIGHT_COLUMNS} FROM records WHERE 1=1{where} "
                     f"ORDER BY record_date DESC, record_time DESC, id DESC", params)
    for row in rows:
        # аналіз іншого типу (тип запису змінили) не показуємо
        row["has_analysis"] = row.get("analysis_kind") == row.get("record_type")
    return rows


def backfill_light_columns(batch: int = 200) -> int:
    """Заповнює легкі поля для записів, проаналізованих старими версіями коду."""
    total = 0
    while True:
        rows = fetch_all("SELECT id, analysis_json FROM records WHERE analysis_json IS NOT NULL "
                         "AND analysis_json != '' AND analysis_kind IS NULL LIMIT ?", (batch,))
        if not rows:
            return total
        for row in rows:
            fields = light_fields_from_json(row["analysis_json"])
            fields["analysis_kind"] = fields["analysis_kind"] or "unknown"  # щоб не обробляти повторно
            assignments = ", ".join(f"{k} = ?" for k in fields)
            # analysis_kind IS NULL: якщо конвеєр щойно записав новий аналіз — не перетираємо його показники
            total += execute(f"UPDATE records SET {assignments} WHERE id = ? AND analysis_kind IS NULL",
                             (*fields.values(), row["id"]))


def get_records_analyzed_since(since_iso: str) -> list[dict]:
    """Записи, проаналізовані після моменту since_iso (UTC) — для щоденного звіту."""
    rows = fetch_all(f"SELECT {_LIST_COLUMNS} FROM records WHERE analyzed_at IS NOT NULL AND analyzed_at > ? "
                     f"ORDER BY analyzed_at", (since_iso,))
    return [_decorate(r) for r in rows]


def _like_pattern(text: str) -> str:
    escaped = text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_records(q: str, person_name: str | None = None, limit: int = 30,
                   include_transcript: bool = True) -> list[dict]:
    """Пошук у транскрипціях, аналізах, коментарях та іменах (без урахування регістру)."""
    pattern = _like_pattern(q)
    fields = ("person_name", "trainer_name", "manager_comment", "summary")
    if include_transcript:
        fields = ("transcription",) + fields
    condition = " OR ".join(f"LOWER(COALESCE({f}, '')) LIKE ? ESCAPE '\\'" for f in fields)
    select = f"{_LIST_COLUMNS}, transcription" if include_transcript else _LIST_COLUMNS
    query = f"SELECT {select} FROM records WHERE ({condition})"
    params = [pattern] * len(fields)
    if person_name is not None:
        query += " AND person_name = ?"
        params.append(person_name)
    query += " ORDER BY record_date DESC, id DESC LIMIT ?"
    params.append(int(limit))
    return [_decorate(r) for r in fetch_all(query, params)]


def rename_person(old: str, new: str) -> int:
    """Обʼєднує/перейменовує людину в усіх записах і привʼязках користувачів."""
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(_sql("UPDATE records SET person_name = ?, auto_detect = 0 WHERE person_name = ?"), (new, old))
        count = cur.rowcount
        cur.execute(_sql("UPDATE records SET trainer_name = ? WHERE trainer_name = ?"), (new, old))
        count += cur.rowcount
        cur.execute(_sql("UPDATE users SET person_name = ? WHERE person_name = ?"), (new, old))
        cur.execute(_sql("UPDATE app_settings SET key = ? WHERE key = ? AND NOT EXISTS "
                         "(SELECT 1 FROM app_settings WHERE key = ?)"), (f"coach:{new}", f"coach:{old}", f"coach:{new}"))
        cur.close()
    return count


def get_person_names(record_type=None) -> list[str]:
    query = "SELECT DISTINCT person_name FROM records WHERE person_name IS NOT NULL AND person_name != ''"
    params = []
    if record_type:
        query += " AND record_type = ?"
        params.append(record_type)
    return [r["person_name"] for r in fetch_all(query + " ORDER BY person_name", params)]


def get_trainer_names_from_sales() -> list[str]:
    rows = fetch_all(
        "SELECT DISTINCT trainer_name FROM records "
        "WHERE record_type = 'sales' AND trainer_name IS NOT NULL AND trainer_name != '' "
        "ORDER BY trainer_name"
    )
    return [r["trainer_name"] for r in rows]


def update_comment(record_id, comment):
    update_record(record_id, manager_comment=comment)


def update_sale_result(record_id, sale_made, sale_amount=None):
    """sale_made: True / False / None (None — скинути результат)."""
    value = None if sale_made is None else (1 if sale_made else 0)
    amount = sale_amount if value == 1 else None
    update_record(record_id, sale_made=value, sale_amount=amount)


def delete_record(record_id) -> dict | None:
    """Видаляє запис і повертає його (щоб викликач міг прибрати файл)."""
    row = fetch_one("SELECT id, filename, source FROM records WHERE id = ?", (record_id,))
    if row:
        execute("DELETE FROM records WHERE id = ?", (record_id,))
    return row


def find_record_by_meeting(meeting_uuid: str) -> dict | None:
    return fetch_one(
        "SELECT id, status, not_before FROM records WHERE zoom_meeting_uuid = ? ORDER BY id DESC LIMIT 1",
        (meeting_uuid,),
    )


def count_records_by_status() -> dict:
    rows = fetch_all("SELECT status, COUNT(*) AS n FROM records GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


# ── Черга обробки ─────────────────────────────────────────────────────────────

def enqueue_record(record_id: int, job_kind: str = "full", not_before: str | None = None) -> bool:
    """Ставить запис у чергу, якщо він зараз не обробляється. False — уже в роботі."""
    return execute(
        "UPDATE records SET status = 'queued', job_kind = ?, error_message = NULL, locked_at = NULL, "
        "not_before = ?, attempts = 0, updated_at = ? "
        "WHERE id = ? AND status NOT IN ('processing', 'analyzing')",
        (job_kind, not_before, utcnow_iso(), record_id),
    ) == 1


_VALID_TRANSCRIPT_SQL = ("transcription IS NOT NULL AND transcription != '' "
                         "AND transcription NOT LIKE '[ПОМИЛКА%'")


REANALYSIS_BATCH_LIMIT = 500


def enqueue_reanalysis(record_type: str, date_from: str | None, date_to: str | None,
                       limit: int = REANALYSIS_BATCH_LIMIT) -> int:
    """
    Масовий переаналіз (після зміни чек-листа): записи з готовою транскрипцією, найсвіжіші
    першими, не більше limit. Імʼя/тип не перевизначаються (auto_detect = 0).
    """
    select = (f"SELECT id FROM records WHERE record_type = ? AND status IN ('done', 'error') "
              f"AND {_VALID_TRANSCRIPT_SQL}")
    params = [record_type]
    if date_from:
        select += " AND record_date >= ?"
        params.append(date_from)
    if date_to:
        select += " AND record_date <= ?"
        params.append(date_to)
    ids = [r["id"] for r in fetch_all(select + " ORDER BY record_date DESC, id DESC LIMIT ?", (*params, int(limit)))]
    if not ids:
        return 0
    marks = ", ".join("?" for _ in ids)
    return execute(
        f"UPDATE records SET status = 'queued', job_kind = 'reanalyze', auto_detect = 0, error_message = NULL, "
        f"locked_at = NULL, not_before = NULL, attempts = 0, updated_at = ? "
        f"WHERE id IN ({marks}) AND status IN ('done', 'error')",
        (utcnow_iso(), *ids),
    )


def count_reanalysis_candidates(record_type: str, date_from: str | None, date_to: str | None) -> int:
    query = (f"SELECT COUNT(*) AS n FROM records WHERE record_type = ? AND status IN ('done', 'error') "
             f"AND {_VALID_TRANSCRIPT_SQL}")
    params = [record_type]
    if date_from:
        query += " AND record_date >= ?"
        params.append(date_from)
    if date_to:
        query += " AND record_date <= ?"
        params.append(date_to)
    return fetch_one(query, params)["n"]


def claim_next_job(source: str | None = None) -> dict | None:
    """Атомарно бере наступний запис з черги (безпечно для кількох процесів)."""
    now = utcnow_iso()
    query = ("SELECT id FROM records WHERE status = 'queued' "
             "AND (not_before IS NULL OR not_before <= ?)")
    # масовий переаналіз — в останню чергу, щоб не затримувати нові дзвінки
    params = [now]
    if source:
        query += " AND source = ?"
        params.append(source)
    for _ in range(5):
        row = fetch_one(query + " ORDER BY CASE WHEN job_kind = 'reanalyze' THEN 1 ELSE 0 END, id LIMIT 1", params)
        if not row:
            return None
        claimed = execute(
            "UPDATE records SET status = 'processing', locked_at = ?, updated_at = ?, "
            "attempts = COALESCE(attempts, 0) + 1 WHERE id = ? AND status = 'queued'",
            (now, now, row["id"]),
        )
        if claimed == 1:
            return get_record(row["id"])
    return None


def heartbeat(record_ids) -> None:
    ids = list(record_ids)
    if not ids:
        return
    marks = ", ".join("?" for _ in ids)
    execute(f"UPDATE records SET locked_at = ? WHERE id IN ({marks})", (utcnow_iso(), *ids))


def requeue_stale_jobs(stale_minutes: int = 5, legacy_minutes: int = 15,
                       max_attempts: int = 3) -> tuple[int, int]:
    """
    Повертає в чергу записи, обробка яких «зависла» (процес помер / Railway перезапустив
    контейнер). Активні задачі оновлюють locked_at щохвилини; записи без locked_at
    (створені старою версією коду) вважаються зависшими через legacy_minutes.
    Після max_attempts спроб запис переходить у error. Повертає (requeued, failed).
    """
    stale_condition = (
        "status IN ('processing', 'analyzing') AND ("
        "(locked_at IS NOT NULL AND locked_at < ?) OR "
        "(locked_at IS NULL AND COALESCE(updated_at, created_at) < ?))"
    )
    cutoffs = (utcnow_iso(timedelta(minutes=-stale_minutes)),
               utcnow_iso(timedelta(minutes=-legacy_minutes)))
    stale = fetch_all(f"SELECT id, attempts FROM records WHERE {stale_condition}", cutoffs)
    requeued = failed = 0
    for row in stale:
        if (row.get("attempts") or 0) >= max_attempts:
            failed += execute(
                f"UPDATE records SET status = 'error', locked_at = NULL, error_message = ?, updated_at = ? "
                f"WHERE id = ? AND {stale_condition}",
                ("Обробку перервано кілька разів (перезапуск сервера). Натисніть «Повторити».",
                 utcnow_iso(), row["id"], *cutoffs),
            )
        else:
            requeued += execute(
                f"UPDATE records SET status = 'queued', locked_at = NULL, updated_at = ? "
                f"WHERE id = ? AND {stale_condition}",
                (utcnow_iso(), row["id"], *cutoffs),
            )
    return requeued, failed


# ── Zoom: дедуплікація ────────────────────────────────────────────────────────

def is_zoom_file_processed(key: str) -> bool:
    return fetch_one("SELECT 1 AS x FROM zoom_processed WHERE zoom_file_id = ?", (key,)) is not None


def any_zoom_key_processed(keys) -> bool:
    keys = [k for k in keys if k]
    if not keys:
        return False
    marks = ", ".join("?" for _ in keys)
    return fetch_one(
        f"SELECT 1 AS x FROM zoom_processed WHERE zoom_file_id IN ({marks}) LIMIT 1", keys
    ) is not None


def claim_zoom_key(key: str) -> bool:
    """Атомарно «забирає» ключ (id файлу або meeting:<uuid>). True — якщо вперше."""
    return execute(
        "INSERT INTO zoom_processed (zoom_file_id, processed_at) VALUES (?, ?) "
        "ON CONFLICT (zoom_file_id) DO NOTHING",
        (key, utcnow_iso()),
    ) == 1


def mark_zoom_file_processed(key: str) -> None:
    claim_zoom_key(key)


def release_zoom_key(key: str) -> int:
    return execute("DELETE FROM zoom_processed WHERE zoom_file_id = ?", (key,))


def zoom_record_filenames() -> list[str]:
    rows = fetch_all("SELECT filename FROM records WHERE filename LIKE 'zoom_%'")
    return [r["filename"] for r in rows if r.get("filename")]


# ── Журнал вебхуків ───────────────────────────────────────────────────────────

def log_webhook(event: str, status: str, details: str) -> None:
    try:
        execute(
            "INSERT INTO webhook_log (received_at, event, status, details) VALUES (?, ?, ?, ?)",
            (utcnow_iso(), event, status, (details or "")[:2000]),
        )
    except Exception:
        log.exception("Не вдалося записати webhook_log")


def get_webhook_logs(limit: int = 50) -> list[dict]:
    return fetch_all(
        "SELECT id, received_at, event, status, details FROM webhook_log ORDER BY id DESC LIMIT ?",
        (int(limit),),
    )


def prune_webhook_logs(keep: int = 2000) -> int:
    row = fetch_one(
        "SELECT id FROM webhook_log ORDER BY id DESC LIMIT 1 OFFSET ?", (int(keep),)
    )
    if not row:
        return 0
    return execute("DELETE FROM webhook_log WHERE id <= ?", (row["id"],))


# ── Журнал подій застосунку (services/applog.py) ──────────────────────────────

LOG_LEVEL_GROUPS = {
    "problems": ("WARNING", "ERROR", "CRITICAL"),
    "errors": ("ERROR", "CRITICAL"),
    "warnings": ("WARNING",),
    "actions": ("INFO",),
}


def log_event(entry: dict, group_minutes: int = 60) -> bool:
    """Записує подію. Повтор тієї ж помилки за group_minutes — +1 до лічильника. True — нова група."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=group_minutes)).isoformat(timespec="seconds")
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(_sql("SELECT id FROM app_logs WHERE fingerprint = ? AND last_at >= ? ORDER BY id DESC LIMIT 1"),
                    (entry["fingerprint"], since))
        row = cur.fetchone()
        if row:
            # Показуємо найсвіжіший приклад (повідомлення, traceback, контекст) і час останнього повтору.
            cur.execute(_sql("UPDATE app_logs SET count = count + 1, last_at = ?, message = ?, details = ?, context = ? "
                             "WHERE id = ?"),
                        (entry["created_at"], entry["message"], entry["details"], entry["context"], row[0]))
        else:
            cur.execute(_sql("INSERT INTO app_logs (created_at, last_at, level, logger, message, details, context, "
                             "fingerprint, count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)"),
                        (entry["created_at"], entry["created_at"], entry["level"], entry["logger"], entry["message"],
                         entry["details"], entry["context"], entry["fingerprint"]))
        cur.close()
    return not row


def _log_filters(group=None, q=None, since=None) -> tuple[str, list]:
    where, params = "", []
    if group in LOG_LEVEL_GROUPS:
        levels = LOG_LEVEL_GROUPS[group]
        where += f" AND level IN ({', '.join('?' for _ in levels)})"
        params += list(levels)
    if q:
        where += " AND (lower(message) LIKE ? ESCAPE '\\' OR lower(logger) LIKE ? ESCAPE '\\' " \
                 "OR lower(context) LIKE ? ESCAPE '\\')"
        params += [_like_pattern(q)] * 3
    if since:
        where += " AND last_at >= ?"
        params.append(since)
    return where, params


def list_logs(group=None, q=None, since=None, limit: int = 50, offset: int = 0) -> list[dict]:
    where, params = _log_filters(group, q, since)
    rows = fetch_all(f"SELECT * FROM app_logs WHERE 1=1{where} ORDER BY last_at DESC, id DESC LIMIT ? OFFSET ?",
                     (*params, int(limit), int(offset)))
    for row in rows:
        try:
            row["context"] = json.loads(row.get("context") or "{}")
        except ValueError:
            row["context"] = {}
    return rows


def count_logs(group=None, q=None, since=None) -> int:
    where, params = _log_filters(group, q, since)
    row = fetch_one(f"SELECT COUNT(*) AS n FROM app_logs WHERE 1=1{where}", params)
    return int(row["n"]) if row else 0


def error_count_since(since_iso: str) -> int:
    row = fetch_one("SELECT COALESCE(SUM(count), 0) AS n FROM app_logs WHERE level IN ('ERROR', 'CRITICAL') "
                    "AND last_at >= ?", (since_iso,))
    return int(row["n"]) if row else 0


def prune_logs(days: int = 30, keep: int = 5000) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    removed = execute("DELETE FROM app_logs WHERE last_at < ?", (cutoff,))
    row = fetch_one("SELECT id FROM app_logs ORDER BY id DESC LIMIT 1 OFFSET ?", (int(keep),))
    if row:
        removed += execute("DELETE FROM app_logs WHERE id <= ?", (row["id"],))
    return removed


def clear_logs() -> int:
    return execute("DELETE FROM app_logs")


# ── Користувачі ───────────────────────────────────────────────────────────────

def get_user_by_email(email):
    return fetch_one("SELECT * FROM users WHERE email = ?", ((email or "").strip().lower(),))


def get_user_by_id(user_id):
    return fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))


def get_all_users():
    return fetch_all("SELECT * FROM users ORDER BY created_at DESC")


def get_linked_person_names() -> set[str]:
    """Імена в записах, привʼязані до користувачів (співробітники)."""
    rows = fetch_all("SELECT DISTINCT person_name FROM users WHERE person_name IS NOT NULL AND person_name != ''")
    return {r["person_name"] for r in rows}


def count_users() -> int:
    return fetch_one("SELECT COUNT(*) AS n FROM users")["n"]


def count_active_admins() -> int:
    return fetch_one(
        "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND is_active = 1"
    )["n"]


def create_user(email, name, password_hash, role="viewer", person_name="") -> bool:
    try:
        insert(
            "INSERT INTO users (email, name, password_hash, role, created_at, person_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (email.lower().strip(), name.strip(), password_hash, role, utcnow_iso(), (person_name or "").strip()),
        )
        return True
    except Exception as e:  # дублікат email тощо
        log.info("create_user(%s) не вдалося: %s", email, e)
        return False


def update_user(user_id, name=None, role=None, is_active=None, password_hash=None, person_name=None):
    fields = {k: v for k, v in (("name", name), ("role", role), ("is_active", is_active),
                                ("password_hash", password_hash), ("person_name", person_name))
              if v is not None}
    if not fields:
        return 0
    assignments = ", ".join(f"{k} = ?" for k in fields)
    return execute(f"UPDATE users SET {assignments} WHERE id = ?", (*fields.values(), user_id))


def delete_user(user_id):
    return execute("DELETE FROM users WHERE id = ?", (user_id,))


# ── Кеш аналітики ─────────────────────────────────────────────────────────────

def get_insights(date_from="", date_to=""):
    row = fetch_one(
        "SELECT * FROM insights_cache WHERE date_from = ? AND date_to = ? "
        "ORDER BY updated_at DESC LIMIT 1",
        (date_from or "", date_to or ""),
    )
    if not row:
        return None
    try:
        row["data"] = json.loads(row["data_json"])
    except (TypeError, ValueError):
        return None
    return row


def save_insights(data, date_from="", date_to=""):
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(_sql("DELETE FROM insights_cache WHERE date_from = ? AND date_to = ?"),
                    (date_from or "", date_to or ""))
        cur.execute(
            _sql("INSERT INTO insights_cache (updated_at, date_from, date_to, data_json) VALUES (?, ?, ?, ?)"),
            (utcnow_iso(), date_from or "", date_to or "", json.dumps(data, ensure_ascii=False)),
        )
        cur.close()
