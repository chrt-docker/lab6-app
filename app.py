import os
import time
import uuid

import psycopg2
import psycopg2.extras
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, abort
from werkzeug.utils import secure_filename

app = Flask(__name__)

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "gallery")
DB_USER = os.environ.get("DB_USER", "gallery")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "gallery")

DB_CONNECT_RETRIES = int(os.environ.get("DB_CONNECT_RETRIES", "10"))
DB_CONNECT_DELAY = float(os.environ.get("DB_CONNECT_DELAY", "2"))

# Директория, куда сохраняются загруженные файлы. В проде это должен быть
# путь, смонтированный как volume, общий с сервисом-прокси, который отдаёт
# файлы напрямую, минуя это приложение.
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/app/uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

os.makedirs(UPLOAD_DIR, exist_ok=True)


def get_connection():
    last_error = None
    for attempt in range(1, DB_CONNECT_RETRIES + 1):
        try:
            return psycopg2.connect(
                host=DB_HOST,
                port=DB_PORT,
                dbname=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
            )
        except psycopg2.OperationalError as exc:
            last_error = exc
            app.logger.warning(
                "Попытка %s/%s подключиться к БД не удалась: %s",
                attempt, DB_CONNECT_RETRIES, exc,
            )
            time.sleep(DB_CONNECT_DELAY)
    raise last_error


def init_db():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS photos (
                    id SERIAL PRIMARY KEY,
                    filename VARCHAR(255) NOT NULL,
                    caption VARCHAR(255) NOT NULL DEFAULT '',
                    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
        conn.commit()
    finally:
        conn.close()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/", methods=["GET"])
def index():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, filename, caption, uploaded_at "
                "FROM photos ORDER BY uploaded_at DESC;"
            )
            photos = cur.fetchall()
    finally:
        conn.close()
    return render_template("index.html", photos=photos)


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("photo")
    caption = request.form.get("caption", "").strip()

    if file is None or file.filename == "" or not allowed_file(file.filename):
        return redirect(url_for("index"))

    ext = secure_filename(file.filename).rsplit(".", 1)[1].lower()
    stored_name = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(UPLOAD_DIR, stored_name))

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO photos (filename, caption) VALUES (%s, %s);",
                (stored_name, caption),
            )
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("index"))


@app.route("/uploads/<path:filename>", methods=["GET"])
def uploaded_file(filename):
    # Приложение умеет отдавать файлы само — это нужно, чтобы оно
    # работало и без reverse-прокси перед ним. Но при развёртывании
    # через nginx эти запросы в норме не должны доходить сюда:
    # nginx должен перехватывать /uploads/ и отдавать файлы напрямую
    # из общего volume, не нагружая процесс Python.
    if not os.path.isfile(os.path.join(UPLOAD_DIR, filename)):
        abort(404)
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/health", methods=["GET"])
def health():
    try:
        conn = get_connection()
        conn.close()
        return {"status": "ok"}, 200
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "detail": str(exc)}, 503


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
