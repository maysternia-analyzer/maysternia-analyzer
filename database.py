import sqlite3
import json
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "data.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
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
    """)
    conn.executescript("""
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
    # Migrations for existing DB
    for col, definition in [
        ("record_time",   "TEXT DEFAULT ''"),
        ("trainer_name",  "TEXT DEFAULT ''"),
        ("sale_made",     "INTEGER DEFAULT NULL"),
        ("sale_amount",   "REAL DEFAULT NULL"),
    ]:
        try:
            conn.execute(f"ALTER TABLE records ADD COLUMN {col} {definition}")
            conn.commit()
        except Exception:
            pass
    conn.close()


def create_record(record_date, record_type, person_name, filename, record_time="", trainer_name=""):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO records (created_at, record_date, record_time, record_type, person_name, trainer_name, filename, status) VALUES (?,?,?,?,?,?,?,?)",
        (datetime.now().isoformat(), record_date, record_time, record_type, person_name, trainer_name, filename, "processing"),
    )
    record_id = cur.lastrowid
    conn.commit()
    conn.close()
    return record_id


def update_sale_result(record_id, sale_made, sale_amount=None):
    conn = get_db()
    conn.execute("UPDATE records SET sale_made=?, sale_amount=? WHERE id=?",
                 (1 if sale_made else 0, sale_amount, record_id))
    conn.commit()
    conn.close()


def get_insights(date_from="", date_to=""):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM insights_cache WHERE date_from=? AND date_to=? ORDER BY updated_at DESC LIMIT 1",
        (date_from, date_to)
    ).fetchone()
    conn.close()
    if not row:
        return None
    r = dict(row)
    r["data"] = json.loads(r["data_json"])
    return r


def get_user_by_email(email):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_users():
    conn = get_db()
    rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_user(email, name, password_hash, role="viewer"):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (email, name, password_hash, role, created_at) VALUES (?,?,?,?,?)",
            (email.lower().strip(), name.strip(), password_hash, role, datetime.now().isoformat())
        )
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


def update_user(user_id, name=None, role=None, is_active=None, password_hash=None):
    conn = get_db()
    fields, values = [], []
    if name is not None: fields.append("name=?"); values.append(name)
    if role is not None: fields.append("role=?"); values.append(role)
    if is_active is not None: fields.append("is_active=?"); values.append(is_active)
    if password_hash is not None: fields.append("password_hash=?"); values.append(password_hash)
    if fields:
        values.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", values)
        conn.commit()
    conn.close()


def delete_user(user_id):
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()


def save_insights(data, date_from="", date_to=""):
    conn = get_db()
    conn.execute("DELETE FROM insights_cache WHERE date_from=? AND date_to=?", (date_from, date_to))
    conn.execute(
        "INSERT INTO insights_cache (updated_at, date_from, date_to, data_json) VALUES (?,?,?,?)",
        (datetime.now().isoformat(), date_from, date_to, json.dumps(data, ensure_ascii=False))
    )
    conn.commit()
    conn.close()


def update_record(record_id, transcription=None, analysis_json=None, status=None):
    conn = get_db()
    fields, values = [], []
    if transcription is not None:
        fields.append("transcription = ?")
        values.append(transcription)
    if analysis_json is not None:
        fields.append("analysis_json = ?")
        values.append(json.dumps(analysis_json, ensure_ascii=False))
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    values.append(record_id)
    conn.execute(f"UPDATE records SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()
    conn.close()


def update_comment(record_id, comment):
    conn = get_db()
    conn.execute("UPDATE records SET manager_comment = ? WHERE id = ?", (comment, record_id))
    conn.commit()
    conn.close()


def get_record(record_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    conn.close()
    if not row:
        return None
    r = dict(row)
    if r.get("analysis_json"):
        r["analysis"] = json.loads(r["analysis_json"])
    return r


def get_all_records(record_type=None, person_name=None, date_from=None, date_to=None):
    conn = get_db()
    query = "SELECT * FROM records WHERE 1=1"
    params = []
    if record_type:
        query += " AND record_type = ?"
        params.append(record_type)
    if person_name:
        query += " AND person_name = ?"
        params.append(person_name)
    if date_from:
        query += " AND record_date >= ?"
        params.append(date_from)
    if date_to:
        query += " AND record_date <= ?"
        params.append(date_to)
    query += " ORDER BY record_date DESC, created_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    result = []
    for row in rows:
        r = dict(row)
        if r.get("analysis_json"):
            r["analysis"] = json.loads(r["analysis_json"])
        result.append(r)
    return result


def get_person_names(record_type=None):
    conn = get_db()
    if record_type:
        rows = conn.execute(
            "SELECT DISTINCT person_name FROM records WHERE record_type=? ORDER BY person_name",
            (record_type,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT DISTINCT person_name FROM records ORDER BY person_name").fetchall()
    conn.close()
    return [r["person_name"] for r in rows]
