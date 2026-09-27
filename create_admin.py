"""
Створення адміністратора (або скидання пароля існуючого користувача).
Запуск: python create_admin.py
"""
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).parent / ".env")
os.environ.setdefault("BACKGROUND_JOBS", "0")

from werkzeug.security import generate_password_hash  # noqa: E402

import database as db  # noqa: E402

MIN_PASSWORD_LENGTH = 8


def main() -> int:
    db.init_db()
    email = input("Email адміністратора: ").strip().lower()
    if not email or "@" not in email:
        print("❌ Некоректний email")
        return 1
    password = getpass.getpass("Пароль (не відображається): ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"❌ Пароль має містити щонайменше {MIN_PASSWORD_LENGTH} символів")
        return 1

    existing = db.get_user_by_email(email)
    if existing:
        answer = input(f"Користувач {email} вже існує. Скинути пароль і зробити адміністратором? [y/N]: ")
        if answer.strip().lower() != "y":
            return 1
        db.update_user(existing["id"], role="admin", is_active=1,
                       password_hash=generate_password_hash(password))
        print(f"✅ Пароль оновлено: {email}")
        return 0

    name = input("Імʼя: ").strip()
    if not name:
        print("❌ Імʼя обовʼязкове")
        return 1
    if db.create_user(email, name, generate_password_hash(password), role="admin"):
        print(f"✅ Адміністратора створено: {email}")
        return 0
    print("❌ Помилка створення")
    return 1


if __name__ == "__main__":
    sys.exit(main())
