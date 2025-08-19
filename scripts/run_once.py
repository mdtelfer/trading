from src.config import load_yaml
from src.router import get_all_mt5_ticks


def main():
    cfg = load_yaml()
    ticks = get_all_mt5_ticks(cfg)
    print("=== MT5 ticks ===")
    for feat, data in ticks.items():
        print(feat, "->", data)


if __name__ == "__main__":
    main()
