"""Structured logging: one JSON object per line, with the request context attached.

``docs/architecture.md`` section 7 asks for ``household_id`` and a request
identifier on every line. Carrying them as function arguments would mean
threading a logger through every call; context variables put them on the
records emitted by libraries too, which is where the interesting lines usually
come from.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any, Final

request_id_var: ContextVar[str | None] = ContextVar("chaudron_request_id", default=None)
household_id_var: ContextVar[str | None] = ContextVar("chaudron_household_id", default=None)

#: Attributes ``logging`` puts on every record; anything else was passed by the
#: caller through ``extra=`` and belongs in the output.
_STANDARD_ATTRS: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render a record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        household_id = household_id_var.get()
        if household_id is not None:
            payload["household_id"] = household_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value
        if record.exc_info is not None:
            # Server-side only: the client never sees a traceback (RFC 9457
            # handler in `chaudron.api.errors`).
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """Install the JSON formatter on the root logger, replacing any earlier one."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs its own colourised handlers; left alone, every access log
    # line would be emitted twice, once structured and once not.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
