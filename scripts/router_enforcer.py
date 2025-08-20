from typing import Any

# Mapea símbolo → grupo (ajústalo a tus Rules.portfolio_groups)
CORE = {"NDX100", "SPX500", "US30", "XAUUSD", "XAGUSD", "DXY", "USDJPY", "EURUSD", "GBPUSD"}
EXTENSION = {"GER30", "JP225", "GBPJPY", "USDCAD"}
GREED = {"BTCUSD", "ETHUSD", "DOGUSD", "ADAUSD", "XRPUSD", "LTCUSD", "XMRUSD"}


def symbol_group(sym: str) -> str:
    s = sym.upper()
    if s in GREED:
        return "GREED"
    if s in EXTENSION:
        return "EXTENSION"
    return "CORE"


def can_execute(
    symbol: str, fused_state: dict[str, Any], open_positions_by_group: dict[str, int]
) -> tuple[bool, str]:
    g = symbol_group(symbol)

    # 1) Grupo elegible
    allowed = fused_state.get("allowed_groups", [])
    if g not in allowed:
        return False, f"blocked_by_group_policy: {g} not in allowed_groups"

    meta = fused_state.get("meta") or {}
    # 2) Caps por grupo
    caps = meta.get("group_caps") or {}
    cap_g = caps.get(g, {}).get("max_open_positions")
    if isinstance(cap_g, int):
        open_cnt = int(open_positions_by_group.get(g, 0))
        if open_cnt >= cap_g:
            return False, f"blocked_by_group_cap: {g} open={open_cnt} cap={cap_g}"

    # 3) Kill-switch para aperturas nuevas (si se usa)
    if meta.get("can_open_new") is False:
        return False, "blocked_by_macro_can_open_new"

    return True, "ok"


def apply_risk_multiplier(base_risk_r: float, fused_state: dict[str, Any]) -> float:
    rm = float(fused_state.get("risk_multiplier", 1.0))
    return max(0.0, base_risk_r * rm)
