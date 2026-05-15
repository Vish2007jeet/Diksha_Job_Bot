"""
Centralised logging using loguru + rich formatting.
"""
import sys
from loguru import logger

logger.remove()
logger.add(
    sys.stderr,
    colorize=True,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{line}</cyan> — <level>{message}</level>",
    level="INFO",
)
logger.add(
    "data/bot.log",
    rotation="10 MB",
    retention="14 days",
    compression="zip",
    level="DEBUG",
    enqueue=True,
)

__all__ = ["logger"]
