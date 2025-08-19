# -*- coding: utf-8 -*-
"""
Descubre símbolos disponibles en tu broker MT5 y mapea aliases canónicos → símbolo real.
Usa credenciales desde .env para conectarse.
"""

import re
import time
from datetime import datetime
import pandas as pd
import MetaTrader5 as mt5
import os
from dotenv import load_dotenv

# ---------- LOAD CREDS ----------

MT5_LOGIN=None
MT5_PASSWORD=None
MT5_SERVER=None

# Convertir login a int si está presente
MT5_LOGIN = int(MT5_LOGIN) if MT5_LOGIN and MT5_LOGIN.isdigit() else None

# ---------- CANONICAL SYMBOLS ----------
CANONICAL = {
    "SPX500": ["US500", "SPX500", "SP500", "US500.cash", "USA500", "SPX"],
    "NDX100": ["NAS100", "USTEC", "US100", "NAS100.cash", "NDX"],
    "US30":   ["US30", "DJ30", "US30.cash", "DJI"],
    "GER30":  ["DE30", "GER30", "GER40", "DE40", "DE30.cash", "GER40.cash"],
    "JP225":  ["JP225", "JPN225", "J225", "JP225.cash"],
    "EURUSD": ["EURUSD"],
    "USDJPY": ["USDJPY"],
    "GBPUSD": ["GBPUSD"],
    "USDCAD": ["USDCAD"],
    "XAUUSD": ["XAUUSD", "GOLD", "GOLDmicro", "XAUUSD.m", "XAUUSD.", "GOLD.cash"],
    "XAGUSD": ["XAGUSD", "SILVER", "XAGUSD.m", "SILVER.cash"],
    "USOIL":  ["USOUSD", "WTI", "WTICO", "XTIUSD", "OIL.WTI", "WTI.cash"],
    "BRENT":  ["BRENT", "UKOIL", "XBRUSD", "BRN", "UKOIL.cash"],
    "BTCUSD": ["BTCUSD", "BTCEUR", "BTCUSD.", "BTCUSDm"],
    "ETHUSD": ["ETHUSD", "ETHUSD.", "ETHUSDm"],
    "ADAUSD": ["ADAUSD", "ADAUSDm"],
    "XRPUSD": ["XRPUSD", "XRPUSDm"],
    "LTCUSD": ["LTCUSD", "LTCUSDm"],
    "DOGEUSD":["DOGEUSD","DOGUSD","DOGEUSDm"],
    "XMRUSD": ["XMRUSD", "XMRUSDm"],
    "VIX":    ["VIX", "VOLX", "VIX.cash", "VIXUSD", "VOLX.cash"],
    "DXY":    ["DXY", "USDX", "DOLLARX", "USDINDEX"],
    "UST10Y": ["UST10Y", "US10Y", "US10YT", "TNX", "US10Y.cash"],
}

def build_regexes(alias_list):
    regs = []
    for a in alias_list:
        escaped = re.escape(a)
        regs.append(re.compile(rf"(^|[^A-Z0-9_.-]){escaped}($|[^A-Z0-9_.-])", re.IGNORECASE))
        regs.append(re.compile(rf"^{escaped}($|[._-])", re.IGNORECASE))
    return regs

CANONICAL_REGEX = {k: build_regexes(v) for k, v in CANONICAL.items()}

def connect_mt5():
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
    if MT5_LOGIN and MT5_PASSWORD and MT5_SERVER:
        if not mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
            raise RuntimeError(f"MT5 login() failed: {mt5.last_error()}")

def fetch_all_symbols():
    infos = mt5.symbols_get()
    if infos is None:
        raise RuntimeError(f"symbols_get() failed: {mt5.last_error()}")
    rows = [{"name": s.name, "path": s.path, "description": s.description} for s in infos]
    return pd.DataFrame(rows)

def find_matches(df_symbols):
    result = {}
    for canon, regex_list in CANONICAL_REGEX.items():
        candidates = []
        for _, row in df_symbols.iterrows():
            target = f"{row['name']}|{row['description']}|{row['path']}".lower()
            if any(r.search(target) for r in regex_list):
                candidates.append(row['name'])
        best = None
        if candidates:
            candidates_sorted = sorted(
                candidates,
                key=lambda s: (".micro" in s.lower(), ".mini" in s.lower(), ".m" in s.lower(),
                               ".pro" in s.lower(), ".cash" in s.lower(), len(s))
            )
            best = candidates_sorted[0]
        result[canon] = {"found": bool(candidates), "symbol": best, "candidates": candidates}
    return result

def get_ticks(symbols):
    out = []
    for sym in symbols:
        if sym and mt5.symbol_select(sym, True):
            tick = mt5.symbol_info_tick(sym)
            if tick:
                out.append({
                    "symbol": sym, "bid": tick.bid, "ask": tick.ask,
                    "time": datetime.fromtimestamp(tick.time).isoformat()
                })
            else:
                out.append({"symbol": sym, "bid": None, "ask": None, "time": None})
    return pd.DataFrame(out)

def main():
    connect_mt5()
    df_symbols = fetch_all_symbols()
    mapping = find_matches(df_symbols)

    print("\n=== MAPEO CANÓNICO → SÍMBOLO DEL BROKER ===")
    rows = []
    for canon, info in mapping.items():
        found = "✅" if info["found"] else "❌"
        print(f"{found} {canon:8s} → {info['symbol'] if info['symbol'] else '—'}")
        rows.append({
            "canonical": canon,
            "found": info["found"],
            "best_symbol": info["symbol"],
            "candidates": ", ".join(info["candidates"])
        })

    df_map = pd.DataFrame(rows).sort_values(["found","canonical"], ascending=[False, True])
    fname = f"mt5_symbol_mapping_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    df_map.to_csv(fname, index=False, encoding="utf-8")
    print(f"\nArchivo guardado: {fname}")

    found_syms = [v["symbol"] for v in mapping.values() if v["symbol"]]
    if found_syms:
        print("\n=== TICKS EN VIVO (bid/ask) ===")
        df_ticks = get_ticks(found_syms)
        print(df_ticks.to_string(index=False))

    missing = [k for k, v in mapping.items() if not v["found"]]
    if missing:
        print("\n=== NO ENCONTRADOS EN MT5 (usar fuente alternativa) ===")
        for m in missing:
            print("-", m)

if __name__ == "__main__":
    try:
        main()
    finally:
        mt5.shutdown()
