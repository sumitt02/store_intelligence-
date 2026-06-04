"""
Structured JSON logging configuration.
Every request logs: trace_id, store_id, endpoint, latency_ms, event_count, status_code.
"""
import json
import logging
import time
import uuid
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


class JSONFormatter(logging.Formatter):
    # Standard LogRecord fields we don't want to re-emit as extras
    _SKIP = frozenset({
        "args", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process",
        "processName", "relativeCreated", "stack_info", "thread",
        "threadName", "created", "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "timestamp": self.formatTime(record, self.datefmt),
        }
        # Merge any extra fields added via logger.info(..., extra={...})
        for key, val in record.__dict__.items():
            if key not in self._SKIP and not key.startswith("_"):
                log_obj[key] = val
        return json.dumps(log_obj)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, logger_name: str = "api.request"):
        super().__init__(app)
        self.logger = logging.getLogger(logger_name)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        trace_id = str(uuid.uuid4())
        request.state.trace_id = trace_id
        start = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception as exc:
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            self.logger.error(
                "unhandled exception",
                extra={"trace_id": trace_id, "endpoint": request.url.path,
                       "latency_ms": latency_ms, "error": str(exc)},
            )
            raise

        latency_ms = round((time.perf_counter() - start) * 1000, 2)

        # Extract store_id from path if present
        path_parts = request.url.path.split("/")
        store_id = None
        if "stores" in path_parts:
            idx = path_parts.index("stores")
            if idx + 1 < len(path_parts):
                store_id = path_parts[idx + 1]

        self.logger.info(
            "request",
            extra={
                "trace_id": trace_id,
                "method": request.method,
                "endpoint": request.url.path,
                "store_id": store_id,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
            }
        )
        response.headers["X-Trace-Id"] = trace_id
        return response
