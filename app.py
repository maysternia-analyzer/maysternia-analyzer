import os
import hmac
import time
import threading
import webbrowser
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, jsonify, abort, send_from_directory, flash
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from dotenv import load_dotenv

load_dotenv()

from database import (init_db, create_record, update_record, update_comment, get_record,
                      get_all_records, get_person_names, update_sale_result, get_insights,
                      save_insights, get_user_by_email, get_user_by_id, get_all_users,
                      create_user, update_user, delete_user)
from services.transcription import transcribe
from services.analysis import analyze
from services.zoom import verify_webhook_signature, download_recording, parse_webhook_payload
from services.detection import detect_type_and_name
from services.poller import start_background_poller, poll_once

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "maysternia-dev-key")

# ── Flask-Login ───────────────────────────────────────────────────────────────
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Будь ласка, увійдіть щоб продовжити"

class User(UserMixin):
    def __init__(self, data):
        self.id = data["id"]
        self.email = data["email"]
        self.name = data["name"]
        self.role = data["role"]
        self.is_active_flag = data.get("is_active", 1)

    @property
    def is_active(self):
        return bool(self.is_active_flag)

    def is_admin(self):
        return self.role == "admin"

@login_manager.user_loader
def load_user(user_id):
    data = get_user_by_id(int(user_id))
    return User(data) if data else None

UPLOAD_FOLDER = Path(__file__).parent / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)
ALLOWED_EXTENSIONS = {"mp4", "m4a", "mp3", "wav", "webm", "mov"}

app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def process_record_async(record_id, file_path, record_type):
    try:
        text = transcribe(file_path)
        update_record(record_id, transcription=text, status="analyzing")
        analysis = analyze(record_type, text)
        update_record(record_id, analysis_json=analysis, status="done")
    except Exception as e:
        update_record(record_id, transcription=f"[ПОМИЛКА]: {e}", status="error")


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First-run setup — only works if no users exist yet."""
    if get_all_users():
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "").strip()
        secret = request.form.get("secret", "")
        if secret != os.environ.get("SECRET_KEY", ""):
            error = "Невірний секретний ключ"
        elif not email or not name or not password:
            error = "Всі поля обов'язкові"
        else:
            ok = create_user(email, name, generate_password_hash(password), role="admin")
            if ok:
                return redirect(url_for("login"))
            error = "Помилка створення"
    return render_template("setup.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user_data = get_user_by_email(email)
        if user_data and user_data.get("is_active") and check_password_hash(user_data["password_hash"], password):
            login_user(User(user_data), remember=True)
            return redirect(request.args.get("next") or url_for("dashboard"))
        error = "Невірний email або пароль"
    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/admin/users")
@login_required
def admin_users():
    if not current_user.is_admin():
        abort(403)
    users = get_all_users()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/create", methods=["POST"])
@login_required
def admin_create_user():
    if not current_user.is_admin():
        abort(403)
    email = request.form.get("email", "").strip()
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "").strip()
    role = request.form.get("role", "viewer")
    if email and name and password:
        create_user(email, name, generate_password_hash(password), role)
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@login_required
def admin_toggle_user(user_id):
    if not current_user.is_admin():
        abort(403)
    if user_id != current_user.id:
        user_data = get_user_by_id(user_id)
        if user_data:
            update_user(user_id, is_active=0 if user_data["is_active"] else 1)
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@login_required
def admin_delete_user(user_id):
    if not current_user.is_admin():
        abort(403)
    if user_id != current_user.id:
        delete_user(user_id)
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/password", methods=["POST"])
@login_required
def admin_change_password(user_id):
    if not current_user.is_admin():
        abort(403)
    password = request.form.get("password", "").strip()
    if password:
        update_user(user_id, password_hash=generate_password_hash(password))
    return redirect(url_for("admin_users"))


# ── Routes ────────────────────────────────────────────────────────────────────

def _build_stats(records):
    sales = [r for r in records if r["record_type"] == "sales" and r.get("analysis")]
    lessons = [r for r in records if r["record_type"] == "lesson" and r.get("analysis")]
    stats = {}
    if sales:
        high = sum(1 for r in sales if r["analysis"].get("deal_chance") == "Високий")
        stats["conversion"] = round(high / len(sales) * 100)
        by_person = {}
        for r in sales:
            p = r["person_name"]
            score = r["analysis"].get("checklist_score", 0)
            by_person.setdefault(p, []).append(score)
        stats["by_person"] = {p: round(sum(v) / len(v)) for p, v in by_person.items()}
    if lessons:
        by_trainer = {}
        for r in lessons:
            p = r["person_name"]
            score = r["analysis"].get("overall_score", 0)
            by_trainer.setdefault(p, []).append(score)
        stats["by_trainer"] = {p: round(sum(v) / len(v)) for p, v in by_trainer.items()}
    return stats


@app.route("/")
@login_required
def dashboard():
    all_records = get_all_records()
    stats = _build_stats(all_records)
    managers = get_person_names("sales")
    trainers = get_person_names("lesson")
    return render_template("index.html", records=all_records, stats=stats,
                           managers=managers, trainers=trainers)


@app.route("/lessons")
@login_required
def lessons_page():
    person_name = request.args.get("person", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    records = get_all_records(record_type="lesson", person_name=person_name or None,
                               date_from=date_from or None, date_to=date_to or None)
    trainers = get_person_names("lesson")
    stats = _build_stats(records)
    return render_template("lessons.html", records=records, trainers=trainers, stats=stats,
                           filters={"person": person_name, "date_from": date_from, "date_to": date_to})


@app.route("/sales")
@login_required
def sales_page():
    manager = request.args.get("manager", "")
    trainer = request.args.get("trainer", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    records = get_all_records(record_type="sales", person_name=manager or None,
                               date_from=date_from or None, date_to=date_to or None)
    # Filter by trainer (in-memory — trainer_name not in DB query yet)
    if trainer:
        records = [r for r in records if r.get("trainer_name") == trainer]
    managers = get_person_names("sales")
    trainers = _get_trainers_from_sales()
    stats = _build_stats(records)
    return render_template("sales.html", records=records, managers=managers, trainers=trainers,
                           stats=stats,
                           filters={"manager": manager, "trainer": trainer,
                                    "date_from": date_from, "date_to": date_to})


def _get_trainers_from_sales():
    from database import get_db, _fetchall, USE_POSTGRES
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT trainer_name FROM records WHERE record_type='sales' AND trainer_name != '' ORDER BY trainer_name"
    )
    rows = _fetchall(cur)
    cur.close()
    conn.close()
    return [r["trainer_name"] for r in rows]


@app.route("/analytics")
@login_required
def analytics_page():
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    cached = get_insights(date_from, date_to)
    return render_template("analytics.html", insights=cached, date_from=date_from, date_to=date_to)


@app.route("/analytics/generate", methods=["POST"])
@login_required
def generate_analytics():
    date_from = request.json.get("date_from", "")
    date_to = request.json.get("date_to", "")
    records = get_all_records(
        date_from=date_from or None,
        date_to=date_to or None,
    )
    from services.insights import generate_insights
    try:
        data = generate_insights(records)
        save_insights(data, date_from, date_to)
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        file = request.files.get("file")
        record_type = request.form.get("record_type")
        person_name = request.form.get("person_name", "").strip()
        trainer_name = request.form.get("trainer_name", "").strip()
        record_date = request.form.get("record_date", "")
        record_time = request.form.get("record_time", "")

        if not file or file.filename == "":
            return render_template("upload.html", error="Файл не вибрано")
        if not allowed_file(file.filename):
            return render_template("upload.html", error="Непідтримуваний формат файлу")
        if not person_name:
            return render_template("upload.html", error="Введіть імʼя")
        if not record_date:
            return render_template("upload.html", error="Вкажіть дату")

        filename = secure_filename(file.filename)
        save_path = UPLOAD_FOLDER / filename
        # Avoid collision
        counter = 1
        while save_path.exists():
            stem = Path(filename).stem
            suffix = Path(filename).suffix
            save_path = UPLOAD_FOLDER / f"{stem}_{counter}{suffix}"
            counter += 1
        file.save(save_path)

        record_id = create_record(record_date, record_type, person_name, save_path.name,
                                   record_time=record_time, trainer_name=trainer_name)
        thread = threading.Thread(target=process_record_async, args=(record_id, str(save_path), record_type), daemon=True)
        thread.start()

        return redirect(url_for("record_detail", record_id=record_id))

    return render_template("upload.html")


@app.route("/record/<int:record_id>")
@login_required
def record_detail(record_id):
    record = get_record(record_id)
    if not record:
        abort(404)
    return render_template("record.html", record=record)


@app.route("/record/<int:record_id>/status")
@login_required
def record_status(record_id):
    record = get_record(record_id)
    if not record:
        abort(404)
    return jsonify({"status": record["status"]})


@app.route("/record/<int:record_id>/sale_result", methods=["POST"])
@login_required
def save_sale_result(record_id):
    data = request.get_json()
    sale_made = data.get("sale_made")  # True/False/None
    sale_amount = data.get("sale_amount")  # float or None
    update_sale_result(record_id, sale_made, sale_amount)
    return jsonify({"ok": True})


@app.route("/record/<int:record_id>/meta", methods=["POST"])
@login_required
def update_meta(record_id):
    data = request.get_json()
    from database import get_db, _p
    conn = get_db()
    cur = conn.cursor()
    p = _p()
    cur.execute(f"UPDATE records SET record_type={p}, person_name={p} WHERE id={p}",
                (data.get("record_type"), data.get("person_name"), record_id))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})


@app.route("/record/<int:record_id>/comment", methods=["POST"])
@login_required
def save_comment(record_id):
    data = request.get_json()
    update_comment(record_id, data.get("comment", ""))
    return jsonify({"ok": True})


@app.route("/record/<int:record_id>/delete", methods=["POST"])
@login_required
def delete_record(record_id):
    from database import get_db, _p
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM records WHERE id={_p()}", (record_id,))
    conn.commit(); cur.close(); conn.close()
    return redirect(url_for("dashboard"))


@app.route("/record/<int:record_id>/reanalyze", methods=["POST"])
@login_required
def reanalyze(record_id):
    record = get_record(record_id)
    if not record or not record.get("transcription"):
        abort(400)
    update_record(record_id, status="analyzing")
    thread = threading.Thread(
        target=lambda: _reanalyze(record_id, record["transcription"], record["record_type"]),
        daemon=True,
    )
    thread.start()
    return jsonify({"ok": True})


@app.route("/stats")
@login_required
def stats_page():
    records = get_all_records()
    done = [r for r in records if r["status"] == "done" and r.get("analysis")]

    managers = {}
    for r in done:
        p = r["person_name"]
        t = r["record_type"]
        a = r.get("analysis", {})
        if p not in managers:
            managers[p] = {"name": p, "sales": [], "lessons": [], "dates": []}
        managers[p]["dates"].append(r["record_date"])
        if t == "sales":
            managers[p]["sales"].append({
                "score": a.get("checklist_score", 0),
                "chance": a.get("deal_chance", ""),
                "pct": a.get("deal_chance_percent", 0),
                "date": r["record_date"],
            })
        else:
            managers[p]["lessons"].append({
                "score": a.get("overall_score", 0),
                "engagement": a.get("engagement_level", ""),
                "date": r["record_date"],
            })

    # Compute averages
    for p, m in managers.items():
        if m["sales"]:
            m["avg_sales_score"] = round(sum(s["score"] for s in m["sales"]) / len(m["sales"]))
            m["avg_deal_pct"] = round(sum(s["pct"] for s in m["sales"]) / len(m["sales"]))
            m["high_chance"] = sum(1 for s in m["sales"] if s["chance"] == "Високий")
        else:
            m["avg_sales_score"] = None
            m["avg_deal_pct"] = None
            m["high_chance"] = 0
        if m["lessons"]:
            m["avg_lesson_score"] = round(sum(l["score"] for l in m["lessons"]) / len(m["lessons"]))
        else:
            m["avg_lesson_score"] = None
        m["total"] = len(m["sales"]) + len(m["lessons"])

    managers_list = sorted(managers.values(), key=lambda x: x["total"], reverse=True)
    return render_template("stats.html", managers=managers_list, total_records=len(done))


@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ── Zoom Webhook ─────────────────────────────────────────────────────────────

@app.route("/zoom/webhook", methods=["POST"])
def zoom_webhook():
    body = request.get_data()
    data = request.get_json(silent=True) or {}

    # Zoom URL validation challenge (one-time on setup)
    if data.get("event") == "endpoint.url_validation":
        token = data["payload"]["plainToken"]
        hash_val = hmac.new(
            os.environ["ZOOM_WEBHOOK_SECRET"].encode(),
            token.encode(), "sha256"
        ).hexdigest()
        return jsonify({"plainToken": token, "encryptedToken": hash_val})

    # Verify signature
    timestamp = request.headers.get("x-zm-request-timestamp", "")
    signature = request.headers.get("x-zm-signature", "")
    print(f"[Webhook] timestamp={timestamp!r} sig={signature!r}", flush=True)
    if not timestamp or not signature:
        print("[Webhook] ❌ Відсутні заголовки підпису", flush=True)
        return jsonify({"error": "missing signature headers"}), 401
    if not verify_webhook_signature(body, timestamp, signature):
        # Log expected vs received for debugging
        import hashlib as _hl
        secret = os.environ.get("ZOOM_WEBHOOK_SECRET", "")
        msg = f"v0:{timestamp}:{body.decode()}"
        expected = "v0=" + __import__('hmac').new(secret.encode(), msg.encode(), _hl.sha256).hexdigest()
        print(f"[Webhook] ❌ Підпис не збігається. Expected={expected[:30]}... Got={signature[:30]}...", flush=True)
        return jsonify({"error": "invalid signature"}), 401

    print(f"[Zoom webhook] event={data.get('event')}")

    if data.get("event") != "recording.completed":
        return jsonify({"ok": True})

    # Log full payload for debugging
    import json as _json
    print("[Zoom payload]", _json.dumps(data, ensure_ascii=False)[:2000])

    recordings = parse_webhook_payload(data)
    print(f"[Zoom] Знайдено файлів для обробки: {len(recordings)}")
    for rec in recordings:
        print(f"[Zoom] Запускаємо: {rec['topic']} | {rec['filename']} | breakout={rec['is_breakout']}")
        thread = threading.Thread(
            target=_process_zoom_recording, args=(rec,), daemon=True
        )
        thread.start()

    return jsonify({"ok": True, "count": len(recordings)})


def _process_zoom_recording(rec: dict):
    """Download a Zoom recording and run the full analysis pipeline."""
    # Create DB record immediately so failures are visible in dashboard
    st = rec["start_time"]
    record_time = st[11:16] if len(st) > 10 else ""
    record_id = create_record(
        st[:10], "sales", rec.get("host_name", "Невідомо"), rec["filename"], record_time=record_time
    )
    update_record(record_id, status="processing")
    print(f"[Zoom] Створено запис ID:{record_id} | {rec['topic']}")

    try:
        print(f"[Zoom] Завантажуємо: {rec['filename']}")
        file_path = download_recording(rec["download_url"], rec["filename"])
        print(f"[Zoom] Файл збережено: {file_path}")
    except Exception as e:
        print(f"[Zoom] ❌ Помилка завантаження: {e}")
        update_record(record_id, transcription=f"[ПОМИЛКА завантаження]: {e}", status="error")
        return

    try:
        text = transcribe(file_path)
        print(f"[Zoom] Транскрипція: {len(text)} символів")
        update_record(record_id, transcription=text, status="analyzing")
    except Exception as e:
        print(f"[Zoom] ❌ Помилка транскрипції: {e}")
        update_record(record_id, transcription=f"[ПОМИЛКА транскрипції]: {e}", status="error")
        return

    try:
        detection = detect_type_and_name(
            topic=rec["topic"], duration=rec["duration"],
            is_breakout=rec["is_breakout"], host_name=rec["host_name"],
            transcript_preview=text[:2000],
        )
        # Update record with correct type and name
        _db = __import__('database')
        conn = _db.get_db()
        cur = conn.cursor()
        p = _db._p()
        cur.execute(f"UPDATE records SET record_type={p}, person_name={p} WHERE id={p}",
                    (detection["record_type"], detection["person_name"], record_id))
        conn.commit(); cur.close(); conn.close()
        print(f"[Zoom] Тип: {detection['record_type']} | Імʼя: {detection['person_name']}")
    except Exception as e:
        print(f"[Zoom] ⚠ Помилка визначення типу: {e}")

    try:
        rec_data = get_record(record_id)
        analysis = analyze(rec_data["record_type"], text)
        update_record(record_id, analysis_json=analysis, status="done")
        print(f"[Zoom] ✅ ID:{record_id} аналіз завершено")
    except Exception as e:
        print(f"[Zoom] ❌ Помилка аналізу: {e}")
        update_record(record_id, status="error",
                      transcription=text + f"\n[ПОМИЛКА аналізу]: {e}")


def _reanalyze(record_id, transcription, record_type):
    try:
        analysis = analyze(record_type, transcription)
        update_record(record_id, analysis_json=analysis, status="done")
    except Exception as e:
        update_record(record_id, status="error")


# ── Run ───────────────────────────────────────────────────────────────────────

def _run_startup():
    """Runs once at startup regardless of how the app is launched (gunicorn or dev)."""
    init_db()
    start_background_poller(interval_minutes=5)
    def _startup_poll():
        time.sleep(5)
        print("[Startup] Перевіряємо нові записи Zoom...", flush=True)
        try:
            n = poll_once()
            print(f"[Startup] Нових записів: {n}", flush=True)
        except Exception as e:
            print(f"[Startup] Помилка: {e}", flush=True)
    threading.Thread(target=_startup_poll, daemon=True).start()

_run_startup()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"\n✅  Майстерня Аналізатор → http://localhost:{port}\n")
    if os.environ.get("OPEN_BROWSER", "1") == "1":
        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    app.run(debug=False, port=port, use_reloader=False)
