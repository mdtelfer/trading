# src/config.py
from __future__ import annotations

import os
from pathlib import Path

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
