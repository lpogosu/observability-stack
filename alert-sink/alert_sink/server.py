"""Webhook receiver for Alertmanager.

Its job is to make notifications visible without a paging provider: every alert
becomes a JSON log line (which Promtail ships into Loki, so alerts are queryable
next to application logs) and stays available on GET /alerts for the Makefile
target that checks what fired during a demo.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final

from alert_sink.model import MalformedNotificationError, parse_notification
from alert_sink.store import AlertStore

MAX_BODY_BYTES: Final = 4 * 1024 * 1024

logger: Final = logging.getLogger("alert-sink")
STORE: Final = AlertStore()


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "alert-sink/1.0"
    protocol_version = "HTTP/1.1"

    def _respond(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Method names are dictated by BaseHTTPRequestHandler's dispatch.
    def do_GET(self) -> None:
        if self.path.startswith("/alerts/firing"):
            self._respond(
                HTTPStatus.OK,
                [alert.as_log_fields() for alert in STORE.firing()],
            )
        elif self.path.startswith("/alerts"):
            self._respond(
                HTTPStatus.OK,
                [alert.as_log_fields() for alert in STORE.recent(limit=100)],
            )
        elif self.path.startswith("/healthz"):
            self._respond(HTTPStatus.OK, {"status": "ok", "stored": len(STORE)})
        else:
            self._respond(HTTPStatus.NOT_FOUND, {"error": "no such path"})

    def do_POST(self) -> None:
        if not self.path.startswith("/webhook/"):
            self._respond(HTTPStatus.NOT_FOUND, {"error": "no such path"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY_BYTES:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "missing or oversized body"})
            return

        raw = self.rfile.read(length)
        try:
            alerts = parse_notification(json.loads(raw))
        except (json.JSONDecodeError, MalformedNotificationError) as exc:
            logger.warning("rejected notification: %s", exc)
            self._respond(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        STORE.add_all(alerts)
        for alert in alerts:
            # WARNING for firing, INFO for resolved: the log level is the first
            # thing a Loki query filters on.
            level = logging.WARNING if alert.status == "firing" else logging.INFO
            logger.log(level, "alert %s", alert.alertname, extra=alert.as_log_fields())
        self._respond(HTTPStatus.OK, {"accepted": len(alerts)})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # The default implementation writes unstructured lines to stderr, which
        # would break the JSON contract Promtail depends on.
        logger.debug(format, *args)


class JsonFormatter(logging.Formatter):
    _RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def main() -> int:
    configure_logging()
    port = int(os.environ.get("ALERT_SINK_PORT", "9095"))
    # Binds on all interfaces because the only reachable address inside the
    # compose network is the container IP, not loopback.
    server = ThreadingHTTPServer(("0.0.0.0", port), WebhookHandler)
    logger.info("alert sink listening", extra={"port": port, "capacity": STORE.capacity})
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
