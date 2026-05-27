# Central logging facade.
# Single module that owns: stdout JSON formatter, request_id + endpoint
# ContextVar binding, the PII strip filter, and the `log` singleton that
# every other module imports.
#
# Every log line carries the fixed field set:
#   level, time, request_id, endpoint, file, message
#
# Usage: from app.src.logs import log
#        log.info("chat_started", chat_id=cid)
import logging
import sys
from contextvars import ContextVar
from typing import Any

from pythonjsonlogger import jsonlogger

# bound per-request by middleware; defaults empty so startup logs still flow
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
endpoint_var: ContextVar[str] = ContextVar("endpoint", default="")
user_uuid_var: ContextVar[str] = ContextVar("user_uuid", default="")
session_id_var: ContextVar[str] = ContextVar("session_id", default="")

# fields the formatter strips even if accidentally passed via extra=
_PII_FIELDS = {"email", "name", "password", "jwt", "authorization", "token"}


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.endpoint = endpoint_var.get()
        record.user_uuid = user_uuid_var.get()
        record.session_id = session_id_var.get()
        for f in _PII_FIELDS:
            if hasattr(record, f):
                setattr(record, f, "<redacted>")
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(filename)s %(message)s "
            "%(request_id)s %(endpoint)s %(user_uuid)s %(session_id)s",
            rename_fields={
                "asctime": "time",
                "levelname": "level",
                "filename": "file",
            },
        )
    )
    handler.addFilter(_ContextFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def bind_request(request_id: str, user_uuid: str = "", session_id: str = "") -> None:
    request_id_var.set(request_id)
    user_uuid_var.set(user_uuid)
    session_id_var.set(session_id)


class _Log:
    """Other modules call log.info/warning/error/exception with a short
    event name + structured fields. No getLogger calls anywhere else."""

    def __init__(self) -> None:
        self._logger = logging.getLogger("app")

    # Python's logging raises KeyError when an `extra=` key collides with a
    # built-in or filter-set LogRecord attribute (e.g. `request_id`,
    # `endpoint`, `message`, `name`). Strip these defensively so a caller
    # passing `log.info(..., request_id=...)` never silently drops the line.
    _RESERVED = frozenset({
        "request_id", "endpoint", "user_uuid", "session_id",
        "message", "asctime", "name", "levelname", "levelno",
        "pathname", "filename", "module", "funcName", "lineno",
        "created", "msecs", "msg", "args", "exc_info", "exc_text",
        "stack_info", "thread", "threadName", "processName", "process",
    })

    def _emit(self, level: int, event: str, **fields: Any) -> None:
        safe = {k: v for k, v in fields.items() if k not in self._RESERVED}
        self._logger.log(level, event, extra=safe)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, **fields)

    def exception(self, event: str, **fields: Any) -> None:
        self._logger.exception(event, extra=fields)

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, **fields)


log = _Log()
