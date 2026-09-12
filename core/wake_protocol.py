# -*- coding: utf-8 -*-
"""Strict protocol for the final response of a Daily Care wake."""
from dataclasses import dataclass
import json
from typing import Literal, Optional


@dataclass(frozen=True)
class WakeOutput:
    """The only two outcomes a Daily Care wake may expose to a user."""

    action: Literal["send", "silent"]
    message: str


def _reject_json_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")


def parse_wake_output(raw: str) -> Optional[WakeOutput]:
    """Parse one complete wake envelope, returning ``None`` on any violation."""
    if not isinstance(raw, str) or not raw.strip():
        return None

    try:
        data = json.loads(
            raw.strip(),
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict) or set(data) != {"action", "message"}:
        return None

    action = data.get("action")
    message = data.get("message")
    if action == "send":
        if not isinstance(message, str) or not message.strip():
            return None
        return WakeOutput(action="send", message=message)
    if action == "silent" and message == "":
        return WakeOutput(action="silent", message="")
    return None
