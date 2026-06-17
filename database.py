import os
import json
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL", "")

USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3
    from pathlib import Path
    DB_PATH = Path(__file__).parent / "data.db"


def get_db():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


def _row_to_dict(row):
    if USE_POSTGRES:
        return dict(row)
    return dict(row)


def _ph(n=1):
    """Return n placeholders: %s for postgres, ? for sqlite."""
    ph = "%s" if USE_POSTGRES else "?"
    return ", ".join([ph] * n)


def _p():
    return "%s" if USE_POSTGRES else "?"


def init_db():
    conn = get_db()
    cur = conn.cursor()

    if USE_POSTGRES:
        conn.autocommit = True
        cur.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id SERIAL PRIMARY KEY,
                created_at TEXT NOT NULL,
                record_date TEXT NOT NULL,
                record_time TEXT DEFAULT '',
                record_type TEXT NOT NULL,
                person_name TEXT NOT NULL,
                trainer_name TEXT DEFAULT '',
                filename TEXT,
                transcription TEXT,
                analysis_json TEXT,
                manager_comment TEXT,
                status TEXT DEFAULT 'pending',
                sale_made INTEGER DEFAULT NULL,
                sale_amount REAL DEFAULT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS insights_cache (
                id SERIAL PRIMARY KEY,
                updated_at TEXT NOT NULL,
                date_from TEXT DEFAULT '',
                date_to TEXT DEFAULT '',
                data_json TEXT NOT NULL
            )
        """)
    else:
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                record_date TEXT NOT NULL,
                record_time TEXT DEFAULT '',
                record_type TEXT NOT NULL,
                person_name TEXT NOT NULL,
                filename TEXT,
                transcription TEXT,
                analysis_json TEXT,
                manager_comment TEXT,
                status TEXT DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS insights_cache (
                id INTEGER PRIMARY KEY,
                updated_at TEXT NOT NULL,
                date_from TEXT DEFAULT '',
                date_to TEXT DEFAULT '',
                data_json TEXT NOT NULL
            );
        """)
        for col, definition in [
            ("record_time",  "TEXT DEFAULT ''"),
            ("trainer_name", "TEXT DEFAULT ''"),
            ("sale_made",    "INTEGER DEFAULT NULL"),
            ("sale_amount",  "REAL DEFAULT NULL"),
        ]:
            try:
                cur.execute(f"ALTER TABLE records ADD COLUMN {col} {definition}")
                conn.commit()
            except Exception:
                pass

    if not USE_POSTGRES:
        conn.commit()
    cur.close()
    conn.close()


def _fetchone(cur):
    row = cur.fetchone()
    if row is None:
        return None
    if USE_POSTGRES:
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    return dict(row)


def _fetchall(cur):
    rows = cur.fetchall()
    if USE_POSTGRES:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]
    return [dict(r) for r in rows]


def create_record(record_date, record_type, person_name, filename, record_time="", trainer_name=""):
    conn = get_db()
    cur = conn.cursor()
    p = _p()
    if USE_POSTGRES:
        cur.execute(
            f"INSERT INTO records (created_at, record_date, record_time, record_type, person_name, trainer_name, filename, status) VALUES ({_ph(8)}) RETURNING id",
            (datetime.now().isoformat(), record_date, record_time, record_type, person_name, trainer_name, filename, "processing"),
        )
        record_id = cur.fetchone()[0]
    else:
        cur.execute(
            f"INSERT INTO records (created_at, record_date, record_time, record_type, person_name, trainer_name, filename, status) VALUES ({_ph(8)})",
            (datetime.now().isoformat(), record_date, record_time, record_type, person_name, trainer_name, filename, "processing"),
        )
        record_id = cur.lastrowid
    conn.commit()
    cur.close()
    conn.close()
    return record_id


def update_sale_result(record_id, sale_made, sale_amount=None):
    conn = get_db()
    cur = conn.cursor()
    p = _p()
    cur.execute(f"UPDATE records SET sale_made={p}, sale_amount={p} WHERE id={p}",
                (1 if sale_made else 0, sale_amount, record_id))
    conn.commit()
    cur.close()
    conn.close()


def get_insights(date_from="", date_to=""):
    conn = get_db()
    cur = conn.cursor()
    p = _p()
    cur.execute(
        f"SELECT * FROM insights_cache WHERE date_from={p} AND date_to={p} ORDER BY updated_at DESC LIMIT 1",
        (date_from, date_to)
    )
    row = _fetchone(cur)
    cur.close()
    conn.close()
    if not row:
        return None
    row["data"] = json.loads(row["data_json"])
    return row


def get_user_by_email(email):
    conn = get_db()
    cur = conn.cursor()
    p = _p()
    cur.execute(f"SELECT * FROM users WHERE email={p}", (email,))
    row = _fetchone(cur)
    cur.close()
    conn.close()
    return row


def get_user_by_id(user_id):
    conn = get_db()
    cur = conn.cursor()
    p = _p()
    cur.execute(f"SELECT * FROM users WHERE id={p}", (user_id,))
    row = _fetchone(cur)
    cur.close()
    conn.close()
    return row


def get_all_users():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users ORDER BY created_at DESC")
    rows = _fetchall(cur)
    cur.close()
    conn.close()
    return rows


def create_user(email, name, password_hash, role="viewer"):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            f"INSERT INTO users (email, name, password_hash, role, created_at) VALUES ({_ph(5)})",
            (email.lower().strip(), name.strip(), password_hash, role, datetime.now().isoformat())
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()


def update_user(user_id, name=None, role=None, is_active=None, password_hash=None):
    conn = get_db()
    cur = conn.cursor()
    p = _p()
    fields, values = [], []
    if name is not None: fields.append(f"name={p}"); values.append(name)
    if role is not None: fields.append(f"role={p}"); values.append(role)
    if is_active is not None: fields.append(f"is_active={p}"); values.append(is_active)
    if password_hash is not None: fields.append(f"password_hash={p}"); values.append(password_hash)
    if fields:
        values.append(user_id)
        cur.execute(f"UPDATE users SET {', '.join(fields)} WHERE id={p}", values)
        conn.commit()
    cur.close()
    conn.close()


def delete_user(user_id):
    conn = get_db()
    cur = conn.cursor()
    p = _p()
    cur.execute(f"DELETE FROM users WHERE id={p}", (user_id,))
    conn.commit()
    cur.close()
    conn.close()


def save_insights(data, date_from="", date_to=""):
    conn = get_db()
    cur = conn.cursor()
    p = _p()
    cur.execute(f"DELETE FROM insights_cache WHERE date_from={p} AND date_to={p}", (date_from, date_to))
    cur.execute(
        f"INSERT INTO insights_cache (updated_at, date_from, date_to, data_json) VALUES ({_ph(4)})",
        (datetime.now().isoformat(), date_from, date_to, json.dumps(data, ensure_ascii=False))
    )
    conn.commit()
    cur.close()
    conn.close()


def update_record(record_id, transcription=None, analysis_json=None, status=None):
    conn = get_db()
    cur = conn.cursor()
    p = _p()
    fields, values = [], []
    if transcription is not None:
        fields.append(f"transcription = {p}")
        values.append(transcription)
    if analysis_json is not None:
        fields.append(f"analysis_json = {p}")
        values.append(json.dumps(analysis_json, ensure_ascii=False))
    if status is not None:
        fields.append(f"status = {p}")
        values.append(status)
    if fields:
        values.append(record_id)
        cur.execute(f"UPDATE records SET {', '.join(fields)} WHERE id = {p}", values)
        conn.commit()
    cur.close()
    conn.close()


def update_comment(record_id, comment):
    conn = get_db()
    cur = conn.cursor()
    p = _p()
    cur.execute(f"UPDATE records SET manager_comment = {p} WHERE id = {p}", (comment, record_id))
    conn.commit()
    cur.close()
    conn.close()


def get_record(record_id):
    conn = get_db()
    cur = conn.cursor()
    p = _p()
    cur.execute(f"SELECT * FROM records WHERE id = {p}", (record_id,))
    row = _fetchone(cur)
    cur.close()
    conn.close()
    if not row:
        return None
    if row.get("analysis_json"):
        row["analysis"] = json.loads(row["analysis_json"])
    return row


def get_all_records(record_type=None, person_name=None, date_from=None, date_to=None):
    conn = get_db()
    cur = conn.cursor()
    p = _p()
    query = "SELECT * FROM records WHERE 1=1"
    params = []
    if record_type:
        query += f" AND record_type = {p}"
        params.append(record_type)
    if person_name:
        query += f" AND person_name = {p}"
        params.append(person_name)
    if date_from:
        query += f" AND record_date >= {p}"
        params.append(date_from)
    if date_to:
        query += f" AND record_date <= {p}"
        params.append(date_to)
    query += " ORDER BY record_date DESC, created_at DESC"
    cur.execute(query, params)
    rows = _fetchall(cur)
    cur.close()
    conn.close()
    result = []
    for r in rows:
        if r.get("analysis_json"):
            r["analysis"] = json.loads(r["analysis_json"])
        result.append(r)
    return result


def get_person_names(record_type=None):
    conn = get_db()
    cur = conn.cursor()
    p = _p()
    if record_type:
        cur.execute(
            f"SELECT DISTINCT person_name FROM records WHERE record_type={p} ORDER BY person_name",
            (record_type,)
        )
    else:
        cur.execute("SELECT DISTINCT person_name FROM records ORDER BY person_name")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r[0] for r in rows]
