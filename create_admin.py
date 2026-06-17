"""
Створення першого адміністратора.
Запуск: python create_admin.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from werkzeug.security import generate_password_hash
from database import init_db, create_user, get_user_by_email

init_db()

email = input("Email адміністратора: ").strip()
name = input("Ім'я: ").strip()
password = input("Пароль: ").strip()

if not email or not password or not name:
    print("❌ Всі поля обов'язкові")
    sys.exit(1)

if get_user_by_email(email):
    print(f"❌ Користувач {email} вже існує")
    sys.exit(1)

ok = create_user(email, name, generate_password_hash(password), role="admin")
if ok:
    print(f"\n✅ Адміністратор створений: {email}")
else:
    print("❌ Помилка створення")
