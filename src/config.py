# src/config.py
from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass
import yaml

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Detecta raíz del proyecto (carpeta donde está este archivo -> subir hasta encontrar .env)
_THIS = Path(__file__).resolve()
ROOT = _THIS.parents[1]  # .../pro_trading
ENV_PATH = ROOT / ".env"


def _load_env_once():
    # Carga .env si existe y si tenemos python-dotenv
    if load_dotenv and ENV_PATH.exists():
        load_dotenv(dotenv_path=str(ENV_PATH), override=False)


# Cargar al importar el módulo
_load_env_once()


def env(key: str, default: str | None = None) -> str | None:
    return os.getenv(key, default)


def load_yaml(path: str | Path = None):
    # Por defecto usamos configs/fundamental_sources.yaml en el root
    if path is None:
        path = ROOT / "configs" / "fundamental_sources.yaml"
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No se encontró YAML: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}



def mt5_creds():
    login = os.getenv("MT5_LOGIN")
    password = os.getenv("MT5_PASSWORD")
    server = os.getenv("MT5_SERVER")
    path = os.getenv("MT5_PATH")
    login_int = int(login) if login and login.isdigit() else None
    return login_int, password, server, path

@dataclass
class TechnicalRules:
    data: dict


    @property
    def timezone(self) -> str:
        return self.data["meta"]["timezone"]


    @property
    def sessions(self) -> dict:
        return self.data.get("sessions_local", {})


    @property
    def gating(self) -> dict:
        return self.data.get("gating", {})


    @property
    def indicators(self) -> dict:
        return self.data.get("indicators", {})


    @property
    def boxes(self) -> dict:
        return self.data.get("boxes", {})


    @property
    def trendlines(self) -> dict:
        return self.data.get("trendlines", {})


    @property
    def structure(self) -> dict:
        return self.data.get("structure", {})


    @property
    def setups(self) -> dict:
        return self.data.get("setups", {})


    @property
    def scoring_weights(self) -> dict:
        return self.data.get("scoring_weights", {})


    @property
    def volatility_bounds_default(self):
        return self.data.get("volatility_bounds_atr_norm_default", [0.0008, 0.012])


    @property
    def volatility_overrides(self) -> dict:
        return self.data.get("volatility_bounds_atr_norm_overrides", {})


    @property
    def risk_and_limits(self) -> dict:
        return self.data.get("risk_and_limits", {})


    @property
    def exec_hygiene(self) -> dict:
        return self.data.get("execution_hygiene", {})




    def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
    return TechnicalRules(data)
