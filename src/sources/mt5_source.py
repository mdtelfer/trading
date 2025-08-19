# src/sources/mt5_source.py
from __future__ import annotations

from datetime import UTC, datetime
import os
import time
from typing import Any, NamedTuple, cast

import MetaTrader5 as mt5

from ..config import mt5_creds
from ..log import get_logger

log = get_logger("mt5_source")


# ---- Tipos locales (alineados con stubs) ------------------------------------
class _Tick(NamedTuple):
    time: int
    bid: float
    ask: float


class _AccountInfo(NamedTuple):
    login: int
    server: str


# ---- Helper de inicialización (evita no-untyped-def) ------------------------
def _try_initialize(path: str | None, use_path: bool) -> bool:
    """
    Intenta mt5.initialize() con o sin ruta del terminal.
    Devuelve True/False según el resultado de MT5.initialize.
    """
    if use_path and path:
        return bool(mt5.initialize(path))  # fuerza tipo bool para mypy
    return bool(mt5.initialize())  # fuerza tipo bool para mypy


# -----------------------------------------------------------------------------
class MT5:
    """Gestor de conexión única a MT5 con reintentos y reconexión."""

    _initialized: bool = False

    @classmethod
    def connect(cls, retries: int = 3, wait_sec: float = 2.0) -> None:  # noqa: C901
        """
        Se adjunta a una sesión MT5 existente si hay una abierta.
        NO cierra el terminal existente. Reintenta con shutdown solo si falla initialize().
        Si hay credenciales en .env, solo hace login si la cuenta/servidor actual no coincide.
        Puedes forzar modo "attach-only" con MT5_ATTACH_ONLY=true en .env
        """
        if cls._initialized:
            return

        attach_only: bool = os.getenv("MT5_ATTACH_ONLY", "false").lower() in ("1", "true", "yes")
        login, pwd, server, path = mt5_creds()

        # 1) Intento inicial SIN cerrar nada
        use_path: bool = bool(path)
        ok: bool = _try_initialize(path, use_path)
        log.info(
            f"[MT5] initialize path={'ON' if use_path else 'OFF'} ok={ok} err={mt5.last_error()}"
        )

        # Si falló initialize con path, probamos sin path (adjuntarnos a terminal abierto)
        if not ok and use_path:
            mt5.shutdown()
            ok = _try_initialize(path, False)
            log.info(f"[MT5] initialize retry w/o path ok={ok} err={mt5.last_error()}")

        # Si aún falla, hacemos reintentos con shutdown entre medias
        attempts: int = 1
        while not ok and attempts < retries:
            attempts += 1
            mt5.shutdown()
            time.sleep(wait_sec * attempts)
            ok = _try_initialize(path, False)
            log.info(
                f"[MT5] initialize attempt {attempts}/{retries} ok={ok} err={mt5.last_error()}"
            )

        if not ok:
            raise RuntimeError(
                f"[MT5] initialize failed after {retries} attempts: {mt5.last_error()}"
            )

        time.sleep(0.3)  # estabiliza IPC

        # 2) Si estamos en attach-only, damos por buena la sesión actual
        if attach_only:
            info = mt5.terminal_info()
            acct = cast(_AccountInfo | None, mt5.account_info())
            log.info(f"[MT5] attach-only: terminal={info} account={acct}")
            cls._initialized = True
            return

        # 3) Si ya hay cuenta y coincide con .env, no relogueamos
        acct = cast(_AccountInfo | None, mt5.account_info())
        if acct and getattr(acct, "login", None) and getattr(acct, "server", None):
            if (not login or acct.login == login) and (not server or acct.server == server):
                log.info(
                    f"[MT5] sesión existente OK: login={acct.login} server={acct.server} (no relogin)"  # noqa: E501
                )
                cls._initialized = True
                return
            else:
                log.info(
                    f"[MT5] sesión existente diferente (actual: {acct.login}@{acct.server}, "
                    f"querida: {login}@{server}) → se intentará login"
                )

        # 4) Hacer login solo si tenemos credenciales completas
        if login and pwd and server:
            ok = bool(mt5.login(login, password=pwd, server=server))  # fuerza bool
            log.info(f"[MT5] login ok={ok} err={mt5.last_error()}")
            if not ok:
                # como último recurso: cerrar y reintentar initialize+login una vez
                mt5.shutdown()
                time.sleep(wait_sec)
                if not _try_initialize(path, False):
                    raise RuntimeError(
                        f"[MT5] initialize failed before login retry: {mt5.last_error()}"
                    )
                time.sleep(0.3)
                ok = bool(mt5.login(login, password=pwd, server=server))  # fuerza bool
                log.info(f"[MT5] login retry ok={ok} err={mt5.last_error()}")
                if not ok:
                    raise RuntimeError(f"[MT5] login failed: {mt5.last_error()}")
        else:
            log.info("[MT5] sin credenciales, usando sesión activa del terminal")

        cls._initialized = True

    @classmethod
    def ensure_connected(cls) -> None:
        """Reconecta si la sesión se perdió en runtime."""
        if not cls._initialized:
            cls.connect()

    @classmethod
    def shutdown(cls) -> None:
        try:
            mt5.shutdown()
        finally:
            cls._initialized = False


def _tick_to_dict(symbol: str, tick: _Tick) -> dict[str, Any]:
    """Convierte el tick MT5 en dict con timestamp UTC ISO8601."""
    ts = datetime.fromtimestamp(tick.time, tz=UTC)
    return {"symbol": symbol, "bid": tick.bid, "ask": tick.ask, "time": ts.isoformat()}


def get_tick(symbol: str) -> dict[str, Any] | None:
    """Obtiene un tick para `symbol` como dict o None si falla."""
    MT5.ensure_connected()

    if not bool(mt5.symbol_select(symbol, True)):  # fuerza bool para mypy
        log.warning(f"[MT5] symbol_select failed: {symbol} (err={mt5.last_error()})")
        # reintento ligero sin shutdown
        time.sleep(0.2)
        if not bool(mt5.symbol_select(symbol, True)):  # fuerza bool
            return None

    raw = mt5.symbol_info_tick(symbol)
    t = cast(_Tick | None, raw)
    if not t:
        log.warning(f"[MT5] symbol_info_tick None: {symbol} (err={mt5.last_error()})")
        return None

    return _tick_to_dict(symbol, t)


def get_ticks(symbols: list[str]) -> dict[str, dict[str, Any] | None]:
    """
    Consulta múltiples símbolos y devuelve {symbol: tick_dict|None}.
    """
    out: dict[str, dict[str, Any] | None] = {}
    for s in symbols:
        out[s] = get_tick(s)
    return out
