# logger.py
# Sets up logging that writes to both stdout (for console) and bot.log.
# Silences noisy third-party libraries.

import logging
from logging.handlers import RotatingFileHandler
import sys
import warnings
from config import LOG_FILE, LOG_LEVEL

# Silence harmless Python deprecation warnings (e.g., from uvloop in Python 3.14+)
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Silence noisy third-party libraries — they spam HTTP request lines
for noisy in ("httpx", "httpcore", "urllib3", "websockets", "web3", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

def setup_logger():
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)

    root = logging.getLogger()
    if root.handlers:
        return root

    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Cap log at 10MB and keep the last 5 rotations to prevent disk bloat over months of 24/7 runtime
    file_h = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    stream_h = logging.StreamHandler(sys.stdout)
    stream_h.setFormatter(fmt)
    root.addHandler(stream_h)

    return root

log = setup_logger()