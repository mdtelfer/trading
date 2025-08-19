# scripts/test_gate.py
from src.gateway.gate import allow_trade

if __name__ == "__main__":
    # Señal de prueba (simula TradingView)
    signal = {
        "symbol": "XAUUSD",
        "side": "buy",
        "entry": 0.0,
        "sl": 0.0,
        "tp": 0.0,
        "source": "cli-test",
    }
    ok, reason = allow_trade(signal)  # <-- esto audita en core.fund_gate_audit
    print({"allowed": ok, "reason": reason})
