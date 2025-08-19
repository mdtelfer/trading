from typing import Dict, Any, List
from .config import load_yaml
from .sources.mt5_source import get_ticks
from src.log import get_logger

log = get_logger("router")

def mt5_features_from_yaml(cfg: Dict[str, Any]) -> Dict[str, str]:
    mapping = {}
    for layer, feats in cfg.get("layers", {}).items():
        for name, spec in feats.items():
            primary = spec.get("primary", {})
            if str(primary.get("type","")).lower() == "mt5":
                symbol = primary.get("symbol")
                if symbol:
                    mapping[name] = symbol
    return mapping

def get_all_mt5_ticks(cfg: Dict[str, Any]) -> Dict[str, Any]:
    feat2sym = mt5_features_from_yaml(cfg)
    symbols = list(feat2sym.values())
    ticks = get_ticks(symbols)
    # mapear de vuelta a feature
    result = {}
    inv = {v:k for k,v in feat2sym.items()}
    for sym, data in ticks.items():
        feat = inv.get(sym, sym)
        result[feat] = data
    return result
