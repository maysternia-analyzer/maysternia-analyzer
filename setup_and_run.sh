#!/bin/bash
set -e

echo ""
echo "🎭 Майстерня Аналізатор — Встановлення"
echo "======================================="
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "❌ Python3 не знайдено. Встановіть Python 3.10+"
  exit 1
fi

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo "❌ Потрібен Python 3.10+ (зараз: $(python3 --version 2>&1))"
  exit 1
fi

# Create .env if missing
if [ ! -f .env ]; then
  cp .env.example .env
  echo "⚠️  Створено .env файл. Відкрийте його і заповніть щонайменше:"
  echo "   ANTHROPIC_API_KEY=sk-ant-..."
  echo "   SECRET_KEY=<довгий випадковий рядок>"
  echo ""
  echo "Після цього запустіть скрипт ще раз."
  open .env 2>/dev/null || true
  exit 0
fi

# Check keys are filled
if grep -qE "^ANTHROPIC_API_KEY=sk-ant-\.\.\.$" .env; then
  echo "⚠️  API ключі ще не заповнені в .env!"
  echo "   Відкрийте файл .env і вставте реальні ключі."
  open .env 2>/dev/null || true
  exit 1
fi

# Create venv if needed
if [ ! -d "venv" ]; then
  echo "📦 Створюємо virtualenv..."
  python3 -m venv venv
fi

echo "📦 Встановлюємо залежності..."
source venv/bin/activate
pip install -q -r requirements.txt

# Перший адміністратор (якщо користувачів ще немає) — python create_admin.py

echo ""
echo "✅ Все готово! Запускаємо..."
echo ""
python app.py
