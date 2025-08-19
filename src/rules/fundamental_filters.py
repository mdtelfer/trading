# src/rules/fundamental_filters.py
def macro_filter(snap) -> bool:
    vix = snap.get("VIX",{}).get("value")
    ust = snap.get("UST10Y",{}).get("value")
    dxy = snap.get("DXY",{}).get("value")
    if vix is None or ust is None or dxy is None:
        return False
    if vix > 28:
        return False
    if ust > 0.055:
        return False
    return True

def sentiment_filter(snap) -> bool:
    return True  # placeholder

# src/rules/fundamental_filters.py
def micro_filter(snap) -> bool:
    # CALENDAR_BLOCK.value: 1 = bloquear; 0 = permitir
    cb = snap.get("CALENDAR_BLOCK",{}).get("value")
    if cb is None:
        return True  # si no hay dato, no bloqueamos
    return cb == 0


def fundamental_gate(snap) -> bool:
    return macro_filter(snap) and sentiment_filter(snap) and micro_filter(snap)
