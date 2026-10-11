# Application logging. Modules get a named logger under "assistant" (assistant.tools,
# assistant.memory, ...) that writes timestamped lines to stderr, which systemd sends to journald.
# The level comes from LOG_LEVEL in .env: DEBUG, INFO (default), WARNING, or ERROR.
import logging

from config import LOG_LEVEL

_root = logging.getLogger("assistant")
if not _root.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _root.addHandler(_handler)
    _root.setLevel(LOG_LEVEL)
    _root.propagate = False  # keep these lines out of uvicorn's handlers


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"assistant.{name}")
