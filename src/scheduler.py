import time
from typing import Callable

def run_poll(fn: Callable[[], None], interval_sec: float = 5.0):
    while True:
        try:
            fn()
        except Exception as e:
            print(f"[scheduler] error: {e}")
        time.sleep(interval_sec)
