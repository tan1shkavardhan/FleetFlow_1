from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.geo import distance_km

INDIA_TZ = ZoneInfo("Asia/Kolkata")
STATIONARY_RADIUS_KM = 0.015
DEFAULT_PROLONGED_STOP_SECONDS = 300


def expected_speed_kmh(when: datetime | None = None) -> float:
    when = when or datetime.now(INDIA_TZ)
    if when.tzinfo is None:
        when = when.replace(tzinfo=INDIA_TZ)
    local = when.astimezone(INDIA_TZ)
    if 8 <= local.hour < 11:
        return 25.0
    if 17 <= local.hour < 21:
        return 20.0
    return 40.0


async def evaluate_delay(
    redis,
    vehicle_id: str,
    speed: float,
    expected: float,
    latitude: float | None = None,
    longitude: float | None = None,
    recorded_at: datetime | None = None,
) -> dict | None:
    """
    Detect slow movement and prolonged stops.

    A configured demo vehicle is classified as BREAKDOWN only after it has
    remained effectively stationary for prolonged_stop_seconds (300 seconds
    for the five-minute simulation).

    Once confirmed, BREAKDOWN is latched. Movement telemetry alone cannot
    clear it; the recovery workflow must explicitly resolve the breakdown.
    """
    now = recorded_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_ts = now.timestamp()

    key = f"movement:state:{vehicle_id}"
    state = await redis.hgetall(key)

    ratio = speed / expected if expected > 0 else 1.0
    low_threshold = float(getattr(settings, "delay_threshold_ratio", 0.4))
    recovery_threshold = float(getattr(settings, "delay_recovery_ratio", 0.6))
    consecutive_limit = int(getattr(settings, "delay_consecutive_limit", 3))
    prolonged_after = int(
        getattr(settings, "prolonged_stop_seconds", DEFAULT_PROLONGED_STOP_SECONDS)
    )
    configured_ids = set(getattr(settings, "breakdown_vehicle_ids", []) or [])

    previous_timestamp = float(state.get("timestamp", now_ts))
    previous_latitude = state.get("latitude")
    previous_longitude = state.get("longitude")

    displacement = None
    if (
        latitude is not None
        and longitude is not None
        and previous_latitude is not None
        and previous_longitude is not None
    ):
        displacement = distance_km(
            float(previous_latitude),
            float(previous_longitude),
            latitude,
            longitude,
        )

    stationary = speed <= 1.0 and (
        displacement is None or displacement <= STATIONARY_RADIUS_KM
    )

    stopped_since_value = state.get("stopped_since")
    if stationary:
        stopped_since = (
            float(stopped_since_value)
            if stopped_since_value not in (None, "")
            else previous_timestamp
        )
    else:
        stopped_since = None

    stopped_seconds = (
        max(0.0, now_ts - stopped_since)
        if stopped_since is not None
        else 0.0
    )

    previously_latched = state.get("breakdown_latched") == "1"
    newly_confirmed = (
        vehicle_id in configured_ids
        and stationary
        and stopped_seconds >= prolonged_after
        and not previously_latched
    )
    breakdown_latched = previously_latched or newly_confirmed

    low = ratio <= low_threshold
    low_count = int(state.get("low_count", "0")) + 1 if low else 0
    delay_active = state.get("delay_active") == "1"

    event = None
    classification = "MOVING"

    if breakdown_latched:
        classification = "BREAKDOWN"
        delay_active = True
        if newly_confirmed:
            event = {
                "transition": "started",
                "vehicle_id": vehicle_id,
                "speed": speed,
                "expected_speed": expected,
                "ratio": round(ratio, 3),
                "consecutive_readings": low_count,
                "movement_classification": "BREAKDOWN",
                "displacement_km": (
                    round(displacement, 5) if displacement is not None else None
                ),
                "stopped_seconds": round(stopped_seconds, 1),
                "breakdown_confirmed": True,
            }
    elif delay_active and ratio >= recovery_threshold:
        # A recovered delay is explicitly classified as MOVING. Without this
        # assignment, the return mapping can reference an unbound local
        # variable when this branch is taken.
        classification = "MOVING"
        event = {
            "transition": "recovered",
            "vehicle_id": vehicle_id,
            "speed": speed,
            "expected_speed": expected,
            "movement_classification": classification,
            "breakdown_confirmed": False,
        }
        delay_active = False
        low_count = 0
    elif low and not delay_active and low_count >= consecutive_limit:
        if stationary and stopped_seconds >= prolonged_after:
            classification = "PROLONGED_STOPPAGE"
        elif displacement is not None and displacement > STATIONARY_RADIUS_KM:
            classification = "TRAFFIC_CONGESTION"
        elif speed > 1:
            classification = "SLOW_MOVEMENT"
        else:
            classification = "STOPPAGE_SUSPECTED"

        event = {
            "transition": "started",
            "vehicle_id": vehicle_id,
            "speed": speed,
            "expected_speed": expected,
            "ratio": round(ratio, 3),
            "consecutive_readings": low_count,
            "movement_classification": classification,
            "displacement_km": (
                round(displacement, 5) if displacement is not None else None
            ),
            "stopped_seconds": round(stopped_seconds, 1),
            "breakdown_confirmed": False,
        }
        delay_active = True
    else:
        if stationary and stopped_seconds >= prolonged_after:
            classification = "PROLONGED_STOPPAGE"
        elif (
            displacement is not None
            and displacement > STATIONARY_RADIUS_KM
            and low
        ):
            classification = "TRAFFIC_CONGESTION"
        elif low and speed > 1:
            classification = "SLOW_MOVEMENT"
        elif speed <= 1:
            classification = "STOPPAGE_SUSPECTED"
        else:
            classification = "MOVING"

    # The latched state takes precedence over all later speed classifications.
    if breakdown_latched:
        classification = "BREAKDOWN"

    mapping = {
        "low_count": low_count,
        "delay_active": int(delay_active),
        "timestamp": now_ts,
        "last_speed": speed,
        "stopped_since": stopped_since if stopped_since is not None else "",
        "movement_classification": classification,
        "breakdown_confirmed": int(breakdown_latched),
        "breakdown_latched": int(breakdown_latched),
        "stopped_seconds": round(stopped_seconds, 1),
    }
    if latitude is not None:
        mapping["latitude"] = latitude
    if longitude is not None:
        mapping["longitude"] = longitude

    await redis.hset(key, mapping={k: str(v) for k, v in mapping.items()})
    await redis.expire(key, 86400)

    if event is not None:
        event["movement_classification"] = classification
        event["breakdown_confirmed"] = breakdown_latched

    return event
