"""
Ручна синхронізація записів із Zoom Cloud.

    python sync_zoom.py            # поставити в чергу нові зустрічі за 3 дні
    python sync_zoom.py 14         # за 14 днів
    python sync_zoom.py 14 --process   # і одразу обробити їх у цьому процесі

Без --process записи обробить запущений сервер (фоновий воркер).
"""
import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).parent / ".env")
os.environ.setdefault("BACKGROUND_JOBS", "0")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

import database as db  # noqa: E402
from services import zoom  # noqa: E402
from services.pipeline import run_pending_jobs  # noqa: E402
from services.poller import poll_once  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Синхронізація записів Zoom")
    parser.add_argument("days", nargs="?", type=int, default=3, help="за скільки днів (за замовчуванням 3)")
    parser.add_argument("--process", action="store_true", help="обробити нові Zoom-записи в цьому процесі")
    args = parser.parse_args()

    db.init_db()
    if not zoom.is_configured():
        print("❌ Zoom не налаштовано: задайте ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET у .env")
        return 1

    summary = poll_once(args.days)
    print(f"\n📋 Zoom за {args.days} дн.: {summary}")
    if args.process:
        processed = run_pending_jobs(source="zoom")
        print(f"✅ Оброблено записів: {processed}")
    else:
        print("ℹ️  Записи обробить сервер. Щоб обробити тут, додайте --process")
    return 0


if __name__ == "__main__":
    sys.exit(main())
