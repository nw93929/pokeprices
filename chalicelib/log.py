import logging
import os


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger.

    Reads LOG_LEVEL from the environment (default INFO). In Lambda, the runtime
    already attaches a handler to the root logger, so we only add a handler
    when running locally (no existing root handlers).
    """
    logger = logging.getLogger(name)
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))

    if not logging.getLogger().handlers and not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            )
        )
        logger.addHandler(handler)
    return logger
