"""
Telling somebody that something happened.

Two sinks: the application log (always on, zero config) and an HTTP webhook
(Slack, Teams, PagerDuty — anything that accepts a JSON POST). Email is
deliberately absent: it needs SMTP configuration nobody has in development,
and every modern alerting destination has a webhook URL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import httpx

logger = logging.getLogger("assarium.alerting")


class EventKind(StrEnum):
    run_succeeded = "run_succeeded"
    run_failed = "run_failed"
    run_quarantine_high = "run_quarantine_high"
    schedule_disabled = "schedule_disabled"
    schedule_resumed = "schedule_resumed"
    new_data_detected = "new_data_detected"


@dataclass(frozen=True, slots=True)
class AlertEvent:
    kind: EventKind
    connection_name: str
    schedule_id: str | None = None
    run_id: str | None = None
    message: str = ""
    details: dict[str, str | int | float | None] | None = None


class AlertSink(Protocol):
    def send(self, event: AlertEvent) -> None: ...


class LogAlertSink:
    """Writes every alert to the application log. Always active."""

    _LEVELS = {
        EventKind.run_succeeded: logging.INFO,
        EventKind.run_failed: logging.ERROR,
        EventKind.run_quarantine_high: logging.WARNING,
        EventKind.schedule_disabled: logging.ERROR,
        EventKind.schedule_resumed: logging.INFO,
        EventKind.new_data_detected: logging.INFO,
    }

    def send(self, event: AlertEvent) -> None:
        level = self._LEVELS.get(event.kind, logging.INFO)
        logger.log(
            level,
            "[%s] %s — %s",
            event.kind.value,
            event.connection_name,
            event.message or "(no message)",
        )


class WebhookAlertSink:
    """POSTs a JSON payload to a configured URL.

    The payload shape follows the Slack incoming-webhook convention (a top-level
    ``text`` key), which Teams and most routing tools also accept.
    """

    def __init__(self, url: str, *, timeout: float = 10.0):
        self._url = url
        self._timeout = timeout

    def send(self, event: AlertEvent) -> None:
        icon = {
            EventKind.run_succeeded: "✅",
            EventKind.run_failed: "❌",
            EventKind.run_quarantine_high: "⚠️",
            EventKind.schedule_disabled: "🛑",
            EventKind.schedule_resumed: "▶️",
            EventKind.new_data_detected: "📥",
        }.get(event.kind, "ℹ️")

        text = f"{icon} *{event.kind.value}* — {event.connection_name}"
        if event.message:
            text += f"\n{event.message}"

        payload: dict = {"text": text}
        if event.details:
            payload["details"] = event.details

        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(self._url, json=payload)
                if response.status_code >= 400:
                    logger.warning(
                        "Webhook returned %d for %s", response.status_code, event.kind.value,
                    )
        except Exception:
            # Alerting must never crash the caller. A log line about the failure is
            # the best we can do — the LogAlertSink already captured the original event.
            logger.exception("Failed to deliver webhook for %s", event.kind.value)


class CompositeAlertSink:
    """Fans out to every configured sink. Failure in one does not stop the rest."""

    def __init__(self, sinks: list[AlertSink]):
        self._sinks = sinks

    def send(self, event: AlertEvent) -> None:
        for sink in self._sinks:
            try:
                sink.send(event)
            except Exception:
                logger.exception("Alert sink %s failed", type(sink).__name__)


_sink: AlertSink | None = None


def get_alert_sink() -> AlertSink:
    global _sink
    if _sink is not None:
        return _sink
    from app.core.config import get_settings

    settings = get_settings()
    sinks: list[AlertSink] = [LogAlertSink()]
    if settings.alert_webhook_url:
        sinks.append(WebhookAlertSink(settings.alert_webhook_url))
    _sink = CompositeAlertSink(sinks)
    return _sink


def send_alert(event: AlertEvent) -> None:
    """Convenience: send an alert through the configured sink."""
    get_alert_sink().send(event)
