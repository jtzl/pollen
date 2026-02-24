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


def get_avg_speed():
    with _lock:
        if _speed_samples:
            return round(sum(_speed_samples) / len(_speed_samples), 1)
        return 0


def get_uptime():
    return int(time.time() - _start_time)
