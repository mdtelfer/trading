# src/utils/io.py
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
from typing import Union

import yaml

PathLike = Union[str, Path]

# Raíz del repo: .../ (asumiendo este archivo vive en src/utils/io.py)
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIGS_DIR = ROOT / "configs"


def _resolve_configs_dir() -> Path:
    """Devuelve la carpeta de configs, con override por env CONFIGS_DIR si existe."""
    env_dir = os.getenv("CONFIGS_DIR")
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        if p.exists() and p.is_dir():
            return p
    return DEFAULT_CONFIGS_DIR


def _coerce_path(path: PathLike | None) -> Path:
    """
    - Si path es None → error (este loader es genérico; decide el caller el default).
    - Si es relativo y existe tal cual → úsalo relativo al CWD.
    - Si es relativo y NO existe → intenta en configs/ (o CONFIGS_DIR).
    - Acepta pasar solo el nombre del yaml: 'technical_rules.yaml'.
    """
    if path is None:
        raise ValueError(
            "load_yaml: se requiere 'path' (o usa un helper como load_technical_rules())."
        )

    p = Path(path).expanduser()
    # Si dieron solo nombre sin separadores, intenta directo en configs/
    if not any(sep in str(p) for sep in (os.sep, "/")) and p.suffix in (".yml", ".yaml"):
        cand = _resolve_configs_dir() / p.name
        if cand.exists():
            return cand.resolve()

    # Si es relativo, probar tal cual y luego en configs/
    if not p.is_absolute():
        if p.exists():
            return p.resolve()
        cand = _resolve_configs_dir() / p
        if cand.exists():
            return cand.resolve()

    return p.resolve()


@lru_cache(maxsize=64)
def _load_yaml_cached(resolved_path: str) -> dict:
    path = Path(resolved_path)
    if not path.exists():
        raise FileNotFoundError(f"No se encontró YAML: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def load_yaml(path: PathLike, *, use_cache: bool = True) -> dict:
    """
    Carga un YAML en un dict.
    - Admite rutas absolutas o relativas.
    - Si relativo y no existe, busca en configs/ (override con $CONFIGS_DIR).
    - Cacheable para lecturas repetidas.
    """
    resolved = _coerce_path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"No se encontró YAML: {resolved}")
    return _load_yaml_cached(str(resolved)) if use_cache else _load_yaml_uncached(resolved)


def _load_yaml_uncached(resolved_path: Path) -> dict:
    with resolved_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


# -------- Helpers opcionales y opinados --------


def load_technical_rules() -> dict:
    """Carga configs/technical_rules.yaml (respeta $CONFIGS_DIR)."""
    return load_yaml("technical_rules.yaml")


def load_swing_rules() -> dict:
    """Carga configs/swing_rules.yaml (respeta $CONFIGS_DIR)."""
    return load_yaml("swing_rules.yaml")
