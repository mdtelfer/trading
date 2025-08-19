from __future__ import annotations

import argparse
import json

from src.gateway.gate import allow_trade


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--side", default="buy")
    ap.add_argument("--entry", type=float, default=0.0)
    ap.add_argument("--sl", type=float, default=0.0)
    ap.add_argument("--tp", type=float, default=0.0)
    args = ap.parse_args()

    signal = dict(
        symbol=args.symbol,
        side=args.side,
        entry=args.entry,
        sl=args.sl,
        tp=args.tp,
        source="cli-test",
    )
    ok, reason = allow_trade(signal)
    print(json.dumps({"allowed": ok, "reason": reason}, ensure_ascii=False))


if __name__ == "__main__":
    main()
