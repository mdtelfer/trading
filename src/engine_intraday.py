# ──────────────────────────────────────────────────────────────────────────────
# src/engine_intraday.py
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

from datetime import datetime

import pandas as pd

from .config import load_technical_rules
from .gating import atr_norm_in_range, spread_ok
from .indicators import atr, ema, vwap
from .scoring import bucket_confidence, score_confluence
from .sessions import session_tag
from .trendlines import bull_trendline_break
from .types import Signal, TechEvent


class IntradayEngine:
    def __init__(self):
        self.rules = load_technical_rules()

    # ───────── Helpers de contexto ─────────
    def _compute_features(
        self, df_m5: pd.DataFrame, df_m15: pd.DataFrame, df_h1: pd.DataFrame
    ) -> dict:
        # EMA H1 para bias
        ema_fast_h1 = ema(df_h1["close"], self.rules.indicators.get("ema_fast", 20))
        ema_mid_h1 = ema(df_h1["close"], self.rules.indicators.get("ema_mid", 50))
        ema_slow_h1 = ema(df_h1["close"], self.rules.indicators.get("ema_slow", 200))
        bias_h1_up = ema_fast_h1.iloc[-1] > ema_mid_h1.iloc[-1] > ema_slow_h1.iloc[-1]

        # ATR y ATR_norm (M15)
        atr_m15 = atr(df_m15, self.rules.indicators.get("atr_n", 14))
        atrn_m15 = atr_m15 / df_m15["close"]

        # VWAP M15
        vwap_m15 = vwap(df_m15)

        return {
            "bias_h1_up": bool(bias_h1_up),
            "atr_m15": float(atr_m15.iloc[-1]) if not pd.isna(atr_m15.iloc[-1]) else None,
            "atrn_m15": float(atrn_m15.iloc[-1]) if not pd.isna(atrn_m15.iloc[-1]) else None,
            "vwap_m15": float(vwap_m15.iloc[-1]) if not pd.isna(vwap_m15.iloc[-1]) else None,
        }

    # ───────── Eventos técnicos principales ─────────
    def _events_brk_rt(
        self, symbol: str, now: datetime, df_m15: pd.DataFrame, boxes_m15: pd.DataFrame
    ) -> list[TechEvent]:
        evs: list[TechEvent] = []
        i = -1
        # breakout_res_confirmed: close > box_top (por cierre), y luego retest_hold: low toca y cierra > mid
        if boxes_m15["box_state"].iloc[i] == "retest_hold":
            evs.append(
                TechEvent(
                    symbol=symbol,
                    tf="M15",
                    event="breakout_res_confirmed+retest_hold",
                    level=float(boxes_m15["box_top"].iloc[i]),
                    session=session_tag(now),
                    ts=now.isoformat(),
                    features={},
                    structure={},
                )
            )
            return evs

    def _events_mom_pb(self, symbol: str, now: datetime, df_m5: pd.DataFrame) -> list[TechEvent]:
        brk = bull_trendline_break(df_m5, slope_eps=self.rules.trendlines.get("slope_eps", 5e-5))
        if brk.iloc[-1]:
            return [
                TechEvent(
                    symbol=symbol,
                    tf="M5",
                    event="tl_bull_break",
                    level=float(df_m5["close"].iloc[-1]),
                    session=session_tag(now),
                    ts=now.isoformat(),
                )
            ]
        return []

    def _events_mr_dyn(
        self, symbol: str, now: datetime, df_m15: pd.DataFrame, vwap_m15: pd.Series
    ) -> list[TechEvent]:
        # toque VWAP - banda simple: close <= vwap y vela de rechazo (cierre > apertura)
        i = -1
        cond_touch = df_m15["close"].iloc[i] <= vwap_m15.iloc[i]
        cond_reject = df_m15["close"].iloc[i] > df_m15["open"].iloc[i]
        if cond_touch and cond_reject:
            return [
                TechEvent(
                    symbol=symbol,
                    tf="M15",
                    event="vwap_touch_reject",
                    level=float(vwap_m15.iloc[i]),
                    session=session_tag(now),
                    ts=now.isoformat(),
                )
            ]

    return []

    # ───────── Promoción a señal ─────────
    def _promote_to_signal_brk_rt(
        self,
        symbol: str,
        now: datetime,
        ctx: dict,
        boxes_m15: pd.DataFrame,
        spread_pct: float,
        atrn_bounds: tuple[float, float],
    ) -> list[Signal]:
        i = -1
        # Gating
        vol_ok = atr_norm_in_range(ctx["atrn_m15"], atrn_bounds)
        spr_ok = spread_ok(spread_pct, self.rules.gating.get("spread_max_percentile_1h", 0.70))
        sess_ok = session_tag(now) in ("LDN", "NY")
        regime_ok = ctx["bias_h1_up"]

        # Confluencia de nivel: precio por encima de VWAP y caja en retest_hold
        level_ok = (boxes_m15["box_state"].iloc[i] == "retest_hold") and (
            df_m15_close := boxes_m15.index
        )
        # candle_ok (placeholder simple): vela verde en M15
        candle_ok = True
        # limpieza (sin news ±30 y mechas no extremas): placeholder True
        cleanliness_ok = True

        features = {
            "regime_h1_ok": regime_ok,
            "level_ok": bool(level_ok),
            "session_ok": sess_ok,
            "volatility_spread_ok": bool(vol_ok and spr_ok),
            "candle_ok": candle_ok,
            "cleanliness_ok": cleanliness_ok,
        }
        score = score_confluence(features, self.rules.scoring_weights)
        if score < self.rules.setups["BRK_RT"].get("min_confluence", 60):
            return []
            conf = bucket_confidence(score)

        entry = boxes_m15["box_mid"].iloc[i]
        sl = min(boxes_m15["box_bot"].iloc[i], entry - ctx["atr_m15"])  # below_box_or_1xATR
        sig = Signal(
            setup_id="BRK_RT",
            symbol=symbol,
            entry=float(entry),
            sl=float(sl),
            tp1_rr=2.0,
            confidence=conf,
            risk_group="core",
            risk_R=1.0,
            session=session_tag(now),
            ts=now.isoformat(),
            meta={"atrn_m15": ctx["atrn_m15"], "spread_p": spread_pct, "score": score},
        )

        return [sig]
