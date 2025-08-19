# src/log.py
from pathlib import Path
import sys

from loguru import logger

LOG_DIR = Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)


def init_logger() -> None:
    """Configura loguru con salida en consola y en archivo rotativo."""
    logger.remove()  # limpia handlers por defecto

    # Consola
    logger.add(
        sys.stdout,
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
        "| <level>{level: <8}</level> "
        "| <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
        "- <level>{message}</level>",
        level="INFO",
        enqueue=True,
        backtrace=True,
        diagnose=True,
    )

    # Archivo rotativo
    logger.add(
        LOG_DIR / "app.log",
        rotation="10 MB",
        retention="7 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
        level="DEBUG",
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )


def get_logger(name: str = "app"):
    """Devuelve un logger nombrado (en realidad loguru es global)."""
    return logger.bind(name=name)
