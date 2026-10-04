"""
storage.py — tiny JSON-file storage backing the new phone-login feature
(auth_otp.py). Zero external dependencies on purpose, matching the rest of
this backend's "runs with nothing but GEMINI_API_KEY" philosophy. Swap for
a real database once you have real users.
"""
import json
import threading
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
_lock = threading.Lock()


def _path(name: str) -> Path:
    return DATA_DIR / f"{name}.json"


def _load(name: str, default):
    p = _path(name)
    if not p.exists():
        return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(name: str, data) -> None:
    with _lock:
        DATA_DIR.mkdir(exist_ok=True)  # tolerate the folder being removed after startup (e.g. a clean deploy)
        with open(_path(name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def get_otps() -> dict:
    return _load("otps", {})


def save_otps(otps: dict) -> None:
    _save("otps", otps)


def get_users() -> dict:
    return _load("users", {})


def save_user(phone: str, record: dict) -> None:
    users = get_users()
    users[phone] = record
    _save("users", users)
