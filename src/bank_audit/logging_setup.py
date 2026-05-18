import logging, os, sys
import structlog

def setup(level: str | None = None) -> structlog.BoundLogger:
    lvl = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    logging.basicConfig(stream=sys.stdout, level=lvl, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, lvl)),
    )
    return structlog.get_logger()
