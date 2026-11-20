from __future__ import annotations

from dataclasses import dataclass
import math
from uuid import UUID


def identifier(value: str) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Expected a canonical conversation/turn UUID")
    return value


def number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class Failure:
    thread_id: str
    turn_id: str
    at: float
    kind: str = "usage_limit"
    origin: str = "desktop"

    def __post_init__(self):
        identifier(self.thread_id)
        identifier(self.turn_id)
        if not number(self.at):
            raise ValueError("Invalid failure time")


@dataclass(frozen=True)
class View:
    latest_turn: str
    status: str = "failed"
    loaded: bool | None = True
    fresh: bool | None = True
    queued_input: bool | None = False
    failure_kind: str = "usage_limit"


@dataclass(frozen=True)
class Quota:
    available: bool | None
    reset_at: float | None = None
    reason: str = "unknown"


def parse_quota(reply) -> Quota:
    """Parse numeric quota windows only; no account, credit or banner is retained.

    Conservatively wait for all exposed blocked buckets. Per-model attribution is
    deliberately not guessed. A past timestamp never overrides usedPercent.
    """
    unknown = Quota(None)
    if not isinstance(reply, dict):
        return unknown
    buckets = reply.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        legacy = reply.get("rateLimits")
        buckets = {"legacy": legacy} if isinstance(legacy, dict) else {}
    blocked, count, explicit = [], 0, False
    for bucket in buckets.values():
        if not isinstance(bucket, dict):
            return unknown
        reached = bucket.get("rateLimitReachedType")
        if bucket.get("spendControlReached") is True or reached in {
            "workspace_owner_credits_depleted", "workspace_member_credits_depleted",
            "workspace_owner_usage_limit_reached", "workspace_member_usage_limit_reached",
        }:
            return Quota(False, reason="billing")
        explicit |= reached == "rate_limit_reached"
        for key in ("primary", "secondary"):
            window = bucket.get(key)
            if window is None:
                continue
            if not isinstance(window, dict):
                return unknown
            used, reset = window.get("usedPercent"), window.get("resetsAt")
            if not number(used) or used < 0:
                return unknown
            if reset is not None and (not number(reset) or reset <= 0):
                return unknown
            count += 1
            if used >= 100:
                blocked.append(reset)
    if blocked:
        reset = max(blocked) if all(x is not None for x in blocked) else None
        return Quota(False, reset, "quota")
    if explicit:
        return Quota(False, reason="quota")
    return Quota(True, reason="available") if count else unknown
