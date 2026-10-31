import json
import os
import threading
import time
import uuid

_DATA_DIR = os.environ.get("FUGUOAI_DATA_DIR", "/opt/fuguoai-server/data")
_PATH = os.path.join(_DATA_DIR, "reminders.json")
_lock = threading.Lock()


def _load():
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(db):
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _PATH)


class ReminderStore:
    """Backend copy of device timer/reminder tasks (contract §8.4).

    The device task list is not part of the states snapshot, so this store is
    the source of truth for the "my reminders" view on the mini-program.
    Persisted to data/reminders.json (host volume) so it survives restarts.
    """

    def add(self, device_id, uid, parameters):
        if not parameters:
            return None
        rid = "r" + uuid.uuid4().hex[:12]
        item = {
            "rid": rid,
            "device_id": device_id,
            "uid": uid,
            "parameters": parameters,
            "created_at": int(time.time()),
        }
        with _lock:
            db = _load()
            db.setdefault(device_id, []).append(item)
            _save(db)
        return rid

    def cancel(self, device_id, rid=None, hour=None, minute=None, repeat=None):
        with _lock:
            db = _load()
            items = db.get(device_id, [])
            before = len(items)
            if rid is not None:
                items = [it for it in items if it.get("rid") != rid]
            else:
                def match(it):
                    p = it.get("parameters", {}) or {}
                    if hour is not None and p.get("hour") != hour:
                        return False
                    if minute is not None and p.get("minute") != minute:
                        return False
                    if repeat is not None and p.get("repeat") != repeat:
                        return False
                    return True
                items = [it for it in items if not match(it)]
            db[device_id] = items
            _save(db)
            return len(items) < before

    def list(self, device_id):
        with _lock:
            db = _load()
            return db.get(device_id, [])


_store = None


def get_reminder_store():
    global _store
    if _store is None:
        _store = ReminderStore()
    return _store
