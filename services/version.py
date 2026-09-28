"""Версія застосунку: файл VERSION у корені (MAJOR.MINOR.PATCH) + коміт, з якого зібрано деплой."""
import os
from pathlib import Path

VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
# Railway підставляє SHA коміту в змінну середовища під час деплою.
COMMIT = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_COMMIT") or "")[:7]


def label() -> str:
    return f"v{VERSION}"
