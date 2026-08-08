import json
import os
import tempfile
import time
import threading
from collections import deque

_speed_samples = deque(maxlen=50)
_lock = threading.Lock()
_start_time = time.time()


def record_generation(tokens, elapsed):
    if elapsed > 0 and tokens > 0:
        with _lock:
            _speed_samples.append(tokens / elapsed)
        # Additive: publish to the shared-state file so a second process
        # (the model-free FastAPI app) can report the same numbers.
        # Done outside the lock because _write_state() takes it itself.
        _write_state()


def get_avg_speed():
    with _lock:
        if _speed_samples:
            return round(sum(_speed_samples) / len(_speed_samples), 1)
        return 0


def get_uptime():
    return int(time.time() - _start_time)


# ===========================================================================
# Shared state across processes (additive - nothing above changed behaviour)
# ===========================================================================
# The Flask worker owns the real speed samples and start time. This file lets
# the model-free FastAPI process report identical tokens_per_second and
# uptime_seconds values without holding a model or its own sample history.
#
# Writes are atomic (temp file in the same directory + os.replace), so a
# reader can never observe a partially written file. Every failure path is
# swallowed: telemetry must never break generation, and readers fall back to
# this process's own in-memory values.
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".speed_state.json")


def _write_state():
    """Atomically publish this process's speed/uptime. Never raises."""
    try:
        with _lock:
            avg = round(sum(_speed_samples) / len(_speed_samples), 1) if _speed_samples else 0
            n_samples = len(_speed_samples)
        payload = {
            "tokens_per_second": avg,
            "start_time": _start_time,
            "updated_at": time.time(),
            "samples": n_samples,
            "pid": os.getpid(),
        }
        directory = os.path.dirname(STATE_PATH) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".speed_state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, STATE_PATH)  # atomic rename
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception:
        pass


def publish():
    """Publish this process's speed/uptime to the shared state file.

    Call from the writing process (the one that also calls record_generation)
    to make its start_time visible before the first generation happens.
    """
    _write_state()


def read_state():
    """Return the shared state dict, or None if missing/unreadable/corrupt."""
    try:
        with open(STATE_PATH, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def get_shared_avg_speed():
    """tokens_per_second as published by the writer; falls back in-process."""
    state = read_state()
    if state is not None:
        value = state.get("tokens_per_second")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return get_avg_speed()


def get_shared_uptime():
    """uptime_seconds measured from the writer's start_time; falls back."""
    state = read_state()
    if state is not None:
        value = state.get("start_time")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(time.time() - value)
    return get_uptime()
