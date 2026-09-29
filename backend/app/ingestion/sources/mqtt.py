"""MQTT ingestion.

Implemented but inert until a broker is configured: `paho-mqtt` is an optional extra, and
a missing broker or a missing library is reported as such rather than raised. Payloads go
through the same `normalize_sample` path as every other source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from backend.app.ingestion.base import ReadingSample, normalize_sample

# Topics a household energy broker is likely to publish readings on.
DEFAULT_TOPICS = ("wattxpense/readings", "home/energy/readings", "energy/readings")


@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    topics: tuple[str, ...] = DEFAULT_TOPICS
    timeout_seconds: float = 3.0

    @classmethod
    def from_env(cls) -> "MqttConfig":
        import os

        topics = os.environ.get("WATTXPENSE_MQTT_TOPICS", "")
        return cls(
            host=os.environ.get("WATTXPENSE_MQTT_HOST", "localhost"),
            port=int(os.environ.get("WATTXPENSE_MQTT_PORT", "1883") or 1883),
            username=os.environ.get("WATTXPENSE_MQTT_USERNAME") or None,
            password=os.environ.get("WATTXPENSE_MQTT_PASSWORD") or None,
            topics=tuple(part.strip() for part in topics.split(",") if part.strip()) or DEFAULT_TOPICS,
        )

    def describe(self) -> dict:
        # Never echo the password back to the browser.
        return {
            "host": self.host,
            "port": self.port,
            "topics": list(self.topics),
            "auth_configured": bool(self.username),
            "requires_password": bool(self.username),
        }


@dataclass
class MqttPollResult:
    status: str
    received: int = 0
    samples: list[ReadingSample] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "received": self.received,
            "errors": self.errors,
            "message": self.message,
        }


def decode_payload(raw: str | bytes) -> dict:
    """Turn one broker message into a raw reading payload."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def poll_once(config: MqttConfig | None = None) -> MqttPollResult:
    """Connect, drain whatever is buffered, and disconnect.

    There is no persistent subscriber on purpose: a short-lived poll is predictable,
    cannot hold a stale connection open, and cannot silently swallow messages while the
    database is being migrated underneath it.
    """
    config = config or MqttConfig.from_env()
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        return MqttPollResult(
            status="unavailable",
            message='MQTT support is not installed. Install it with: pip install -e ".[mqtt]"',
        )

    messages: list[tuple[str, str]] = []
    result = MqttPollResult(status="ok")

    try:
        client = mqtt.Client()
    except TypeError:  # paho-mqtt 1.x signature
        client = mqtt.Client()
    if config.username:
        client.username_pw_set(config.username, config.password)

    def on_connect(mqtt_client, userdata, flags, reason_code, properties=None):
        for topic in config.topics:
            mqtt_client.subscribe(topic)

    def on_message(mqtt_client, userdata, message):
        messages.append((message.topic, message.payload))

    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(config.host, config.port, keepalive=int(config.timeout_seconds))
    except Exception as exc:  # noqa: BLE001 - a broker being down is a normal outcome
        return MqttPollResult(status="unreachable", message=f"Could not reach the broker at {config.host}: {exc}")

    try:
        client.loop_start()
        import time

        time.sleep(max(config.timeout_seconds, 0.1))
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    result.received = len(messages)
    for index, (topic, payload) in enumerate(messages):
        raw: Any = decode_payload(payload)
        if not raw:
            result.errors.append({"index": index, "reason": f"{topic}: payload is not a JSON object"})
            continue
        raw.setdefault("device_id", raw.get("device") or raw.get("meter"))
        try:
            result.samples.append(normalize_sample(raw, "mqtt"))
        except Exception as exc:  # noqa: BLE001
            result.errors.append({"index": index, "reason": f"{topic}: {exc}"})

    if not messages:
        result.message = f"No messages were buffered on {', '.join(config.topics)}."
    return result
