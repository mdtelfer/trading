from src.config import load_yaml
from src.ingestor import ingest_ticks
from src.router import get_all_mt5_ticks
from src.scheduler import run_poll


def job():
    cfg = load_yaml()
    ticks = get_all_mt5_ticks(cfg)
    ingest_ticks(ticks)
    print("saved batch:", ticks.keys())


if __name__ == "__main__":
    run_poll(job, interval_sec=5.0)
