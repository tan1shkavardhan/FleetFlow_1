
from datetime import datetime, timezone
from uuid import uuid4
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import AlertRecord, Geofence, Package, Telemetry, Vehicle
from app.redis_client import redis_client
from app.schemas import TelemetryIn
from app.services.delay_detector import evaluate_delay, expected_speed_kmh
from app.services.geo import distance_km
from app.services.recovery import (
    analyze_recovery,
    execute_recovery,
    get_active_recovery,
)
from app.services.websocket_manager import manager
from app.services.recovery_lifecycle import process_recovery_telemetry

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

logger = logging.getLogger(__name__)

ACTIVE_PACKAGE_STATUSES = ("PENDING", "IN_TRANSIT", "REASSIGNED")

# A failed automatic attempt is retried after this cooldown.
RECOVERY_RETRY_SECONDS = 60

# Successful recovery plans are protected from duplicate creation.
RECOVERY_SUCCESS_LOCK_SECONDS = 86400

# Prevents simultaneous telemetry requests from starting duplicate analysis.
RECOVERY_PROCESSING_LOCK_SECONDS = 300


async def _alert(
    db: AsyncSession,
    vehicle_id: str,
    alert_type: str,
    message: str,
    details: dict,
) -> dict:
    event = {
        "type": alert_type,
        "alert_id": f"alert_{uuid4().hex[:12]}",
        "vehicle_id": vehicle_id,
        "message": message,
        "details": details,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    db.add(
        AlertRecord(
            id=event["alert_id"],
            vehicle_id=vehicle_id,
            alert_type=alert_type,
            message=message,
            details=details,
        )
    )

    return event


async def _broadcast_recovery_event(event: dict) -> None:
    """Broadcast notifications without failing telemetry ingestion."""
    try:
        await manager.broadcast(event)
    except Exception:
        logger.exception(
            "Failed to broadcast event: %s",
            event.get("type"),
        )


async def _set_recovery_lock(
    key: str,
    value: str,
    expiry: int,
) -> None:
    await redis_client.set(
        key,
        value,
        ex=expiry,
    )


async def _start_automatic_recovery(
    db: AsyncSession,
    delayed_vehicle_id: str,
    latitude: float,
    longitude: float,
) -> dict:
    """
    Analyze and execute recovery for one incident vehicle.

    This function is deliberately scoped to one delayed vehicle.
    Its packages, deliveries, helper selection and recovery plan are
    handled independently of other breakdown incidents.
    """

    existing = await get_active_recovery(
        db,
        delayed_vehicle_id,
    )

    if existing:
        return {
            "type": "recovery_status_updated",
            **existing,
            "automatic": True,
            "already_exists": True,
            "message": (
                f"Recovery is already assigned to "
                f"{existing['helper_vehicle_id']}."
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    package_result = await db.execute(
        select(Package).where(
            Package.assigned_vehicle_id == delayed_vehicle_id,
            Package.status.in_(ACTIVE_PACKAGE_STATUSES),
        )
    )

    packages = package_result.scalars().all()

    if not packages:
        return {
            "type": "recovery_not_required",
            "delayed_vehicle_id": delayed_vehicle_id,
            "reason": "NO_UNDELIVERED_PACKAGES",
            "package_ids": [],
            "message": (
                f"{delayed_vehicle_id} has no undelivered packages."
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    package_ids = [package.id for package in packages]

    analysis = await analyze_recovery(
        db=db,
        delayed_vehicle_id=delayed_vehicle_id,
        package_ids=package_ids,
        delivery_ids=[],
        radius_km=20.0,
        max_candidates=200,
        origin_latitude=latitude,
        origin_longitude=longitude,
        apply_accessibility=True,
    )

    candidates = analysis.get("candidates", [])

    feasible_candidates = [
        candidate
        for candidate in candidates
        if candidate.get("feasible")
        and candidate.get("route", {}).get("compatible")
    ]

    notification = {
        "type": "recovery_candidate_notification",
        "delayed_vehicle_id": delayed_vehicle_id,
        "recipient_vehicle_ids": [
            candidate["vehicle_id"]
            for candidate in feasible_candidates
        ],
        "candidate_count": len(feasible_candidates),
        "package_ids": package_ids,
        "origin": analysis.get("origin"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    await _broadcast_recovery_event(notification)

    recommended = analysis.get("recommended_candidate")

    if not recommended:
        return {
            "type": "recovery_unavailable",
            "delayed_vehicle_id": delayed_vehicle_id,
            "package_ids": package_ids,
            "candidate_count": len(candidates),
            "feasible_candidate_count": 0,
            "message": (
                "No nearby helper currently satisfies cargo capacity, "
                "3D packing, and route-detour constraints. "
                "The incident will be retried automatically."
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    result = await execute_recovery(
        db=db,
        delayed_vehicle_id=delayed_vehicle_id,
        helper_vehicle_id=recommended["vehicle_id"],
        package_ids=package_ids,
        delivery_ids=analysis.get("delivery_ids", []),
        plan=analysis,
    )

    return {
        "type": "recovery_status_updated",
        "recovery_plan_id": result["recovery_plan_id"],
        "delayed_vehicle_id": delayed_vehicle_id,
        "helper_vehicle_id": result["helper_vehicle_id"],
        "status": result["status"],
        "package_ids_pending_handover": (
            result["pending_transfer_package_ids"]
        ),
        "delivery_ids_pending_handover": (
            result["pending_transfer_delivery_ids"]
        ),
        "cargo_transfer_status": result["cargo_transfer_status"],
        "route_compatibility": result["route_compatibility"],
        "automatic": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def _handle_recovery_attempt_result(
    breakdown_key: str,
    recovery_event: dict,
) -> None:
    """
    Apply the correct idempotency policy for the recovery result.

    Unavailable:
        Short cooldown; later telemetry may retry.

    Not required:
        Long lock because there is no cargo to recover.

    Plan created or already active:
        Long lock to prevent duplicate plan creation.
    """

    event_type = recovery_event.get("type")

    if event_type == "recovery_unavailable":
        await _set_recovery_lock(
            breakdown_key,
            "retry_after_failure",
            RECOVERY_RETRY_SECONDS,
        )

    elif event_type == "recovery_failed":
        await _set_recovery_lock(
            breakdown_key,
            "retry_after_failure",
            RECOVERY_RETRY_SECONDS,
        )

    elif event_type == "recovery_not_required":
        await _set_recovery_lock(
            breakdown_key,
            "recovery_not_required",
            RECOVERY_SUCCESS_LOCK_SECONDS,
        )

    else:
        await _set_recovery_lock(
            breakdown_key,
            "recovery_created",
            RECOVERY_SUCCESS_LOCK_SECONDS,
        )


@router.post("/")
async def ingest_telemetry(
    payload: TelemetryIn,
    db: AsyncSession = Depends(get_db),
):
    vehicle = await db.get(
        Vehicle,
        payload.vehicle_id,
    )

    if not vehicle:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Vehicle '{payload.vehicle_id}' is not registered. "
                "Register it first via POST /vehicles/."
            ),
        )

    if not vehicle.active:
        raise HTTPException(
            status_code=409,
            detail="Vehicle is inactive",
        )

    recorded_at = payload.timestamp or datetime.now(timezone.utc)

    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)

    # Ignore stale/out-of-order telemetry before it can move the current
    # vehicle position or reverse a geofence transition.
    latest_state = await redis_client.hgetall(
        f"vehicle:latest:{payload.vehicle_id}"
    )

    latest_timestamp = latest_state.get("timestamp")

    if latest_timestamp:
        try:
            latest_dt = datetime.fromisoformat(
                latest_timestamp.replace("Z", "+00:00")
            )

            if latest_dt.tzinfo is None:
                latest_dt = latest_dt.replace(tzinfo=timezone.utc)

            if recorded_at <= latest_dt:
                return {
                    "success": True,
                    "accepted": False,
                    "reason": "stale_or_out_of_order",
                    "vehicle_id": payload.vehicle_id,
                    "recorded_at": recorded_at.isoformat(),
                    "latest_recorded_at": latest_dt.isoformat(),
                }

        except ValueError:
            logger.warning(
                "Invalid cached telemetry timestamp for %s",
                payload.vehicle_id,
            )

    # Event idempotency.
    event_key = None

    if payload.event_id:
        event_key = (
            f"telemetry:event:{payload.vehicle_id}:"
            f"{payload.event_id}"
        )

        claimed = await redis_client.set(
            event_key,
            "processing",
            ex=7 * 24 * 3600,
            nx=True,
        )

        if not claimed:
            return {
                "success": True,
                "accepted": False,
                "reason": "duplicate_event",
                "vehicle_id": payload.vehicle_id,
                "event_id": payload.event_id,
                "recorded_at": recorded_at.isoformat(),
            }

    row = Telemetry(
        vehicle_id=payload.vehicle_id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        speed=payload.speed,
        heading=payload.heading,
        recorded_at=recorded_at,
    )

    db.add(row)

    events = []
    trigger_recovery = False

    # This key is unique to each incident vehicle.
    breakdown_key = (
        f"breakdown:alerted:{payload.vehicle_id}"
    )

    try:
        await db.flush()

        # --------------------------------------------------
        # GEOFENCE PROCESSING
        # --------------------------------------------------

        geofences = (
            await db.execute(
                select(Geofence).where(
                    Geofence.active.is_(True)
                )
            )
        ).scalars().all()

        for fence in geofences:
            distance = distance_km(
                payload.latitude,
                payload.longitude,
                fence.latitude,
                fence.longitude,
            ) * 1000

            inside = distance <= fence.radius_m

            key = (
                f"geofence:state:"
                f"{payload.vehicle_id}:{fence.id}"
            )

            previous = await redis_client.get(key)

            current = "inside" if inside else "outside"

            await redis_client.set(
                key,
                current,
                ex=60 * 60 * 24 * 30,
            )

            # First observation establishes state, not an entry event.
            if previous is not None and previous != current:
                transition = (
                    "ENTER" if current == "inside" else "EXIT"
                )

                events.append(
                    await _alert(
                        db,
                        payload.vehicle_id,
                        "geofence_alert",
                        (
                            f"{payload.vehicle_id} "
                            f"{transition.lower()}ed {fence.name}"
                        ),
                        {
                            "geofence_id": fence.id,
                            "geofence_name": fence.name,
                            "transition": transition,
                            "latitude": payload.latitude,
                            "longitude": payload.longitude,
                            "radius_m": fence.radius_m,
                        },
                    )
                )

        # --------------------------------------------------
        # MOVEMENT AND DELAY DETECTION
        # --------------------------------------------------

        expected = expected_speed_kmh(recorded_at)

        movement = await evaluate_delay(
            redis_client,
            payload.vehicle_id,
            payload.speed,
            expected,
            payload.latitude,
            payload.longitude,
            recorded_at,
        )

        if movement is None:
            detector_state = await redis_client.hgetall(
                f"movement:state:{payload.vehicle_id}"
            )

            stopped_since = detector_state.get("stopped_since")
            state_timestamp = detector_state.get("timestamp")

            stopped_seconds = 0.0

            if stopped_since and state_timestamp:
                stopped_seconds = max(
                    0.0,
                    float(state_timestamp) - float(stopped_since),
                )

            movement = {
                "transition": "none",
                "movement_classification": detector_state.get(
                    "movement_classification",
                    "MOVING",
                ),
                "breakdown_confirmed": (
                    detector_state.get("breakdown_confirmed") == "1"
                ),
                "stopped_seconds": stopped_seconds,
            }

        classification = movement.get(
            "movement_classification",
            "MOVING",
        )

        breakdown_confirmed = bool(
            movement.get("breakdown_confirmed")
            or classification == "BREAKDOWN"
        )

        # --------------------------------------------------
        # ALERT GENERATION
        # --------------------------------------------------

        if movement.get("transition") == "started":
            if breakdown_confirmed:
                events.append(
                    await _alert(
                        db,
                        payload.vehicle_id,
                        "breakdown_alert",
                        (
                            "Prolonged stationary breakdown "
                            f"confirmed for {payload.vehicle_id}"
                        ),
                        movement,
                    )
                )

            else:
                events.append(
                    await _alert(
                        db,
                        payload.vehicle_id,
                        "delay_alert",
                        (
                            f"Movement anomaly detected for "
                            f"{payload.vehicle_id}: {classification}"
                        ),
                        movement,
                    )
                )

        if movement.get("transition") == "recovered":
            await redis_client.delete(breakdown_key)

            events.append(
                await _alert(
                    db,
                    payload.vehicle_id,
                    "delay_recovered",
                    (
                        f"Delay condition cleared for "
                        f"{payload.vehicle_id}"
                    ),
                    movement,
                )
            )

        # --------------------------------------------------
        # VEHICLE STATUS AND RECOVERY LOCK
        # --------------------------------------------------

        if breakdown_confirmed:
            vehicle.status = "BREAKDOWN"

            # If the previous attempt had no feasible helper,
            # remove the expired retry marker and attempt again.
            lock_value = await redis_client.get(breakdown_key)

            if lock_value == "retry_after_failure":
                await redis_client.delete(breakdown_key)
                lock_value = None

            acquired = await redis_client.set(
                breakdown_key,
                "processing",
                ex=RECOVERY_PROCESSING_LOCK_SECONDS,
                nx=True,
            )

            trigger_recovery = bool(acquired)

            if acquired and movement.get("transition") != "started":
                events.append(
                    await _alert(
                        db,
                        payload.vehicle_id,
                        "breakdown_alert",
                        (
                            "Prolonged stationary breakdown "
                            f"confirmed for {payload.vehicle_id}"
                        ),
                        movement,
                    )
                )

        elif classification == "PROLONGED_STOPPAGE":
            if vehicle.status != "BREAKDOWN":
                vehicle.status = "STOPPED"

        elif classification in {
            "TRAFFIC_CONGESTION",
            "SLOW_MOVEMENT",
        }:
            if vehicle.status != "BREAKDOWN":
                vehicle.status = "DELAYED"

        elif payload.speed > 1:
            # A breakdown remains BREAKDOWN until the recovery workflow
            # explicitly resolves it. Movement alone is not recovery.
            if vehicle.status in {
                "STOPPED",
                "DELAYED",
            }:
                vehicle.status = "MOVING"

        # --------------------------------------------------
        # DATABASE COMMIT
        # --------------------------------------------------

        await db.commit()

        if event_key:
            await redis_client.set(
                event_key,
                "processed",
                ex=7 * 24 * 3600,
            )

        # --------------------------------------------------
        # REDIS CURRENT LOCATION
        # --------------------------------------------------

        await redis_client.geoadd(
            settings.vehicle_geo_key,
            (
                payload.longitude,
                payload.latitude,
                payload.vehicle_id,
            ),
        )

        vehicle_update = {
            "vehicle_id": payload.vehicle_id,
            "latitude": payload.latitude,
            "longitude": payload.longitude,
            "speed": payload.speed,
            "heading": payload.heading,
            "status": vehicle.status,
            "timestamp": recorded_at.isoformat(),
            "movement_classification": classification,
        }

        await redis_client.hset(
            f"vehicle:latest:{payload.vehicle_id}",
            mapping={
                key: "" if value is None else str(value)
                for key, value in vehicle_update.items()
            },
        )

        # --------------------------------------------------
        # REDIS STREAM EVENT
        # --------------------------------------------------

        await redis_client.xadd(
            settings.telemetry_stream_key,
            {
                "event_type": "vehicle_update",
                "vehicle_id": payload.vehicle_id,
                "vehicle": json.dumps(vehicle_update),
            },
            maxlen=settings.telemetry_stream_maxlen,
            approximate=True,
        )

        # --------------------------------------------------
        # BROADCAST ALERTS
        # --------------------------------------------------

        for event in events:
            await _broadcast_recovery_event(event)

    except HTTPException:
        await db.rollback()

        if event_key:
            await redis_client.delete(event_key)

        raise

    except Exception as exc:
        await db.rollback()

        if event_key:
            try:
                await redis_client.delete(event_key)
            except Exception:
                logger.exception(
                    "Could not release telemetry idempotency key"
                )

        logger.exception(
            "Telemetry processing failed for %s",
            payload.vehicle_id,
        )

        raise HTTPException(
            status_code=503,
            detail=(
                f"Telemetry processing failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc

    # ------------------------------------------------------
    # AUTOMATIC RECOVERY
    # ------------------------------------------------------

    recovery_event = None

    if trigger_recovery:
        try:
            recovery_event = await _start_automatic_recovery(
                db,
                payload.vehicle_id,
                payload.latitude,
                payload.longitude,
            )

            # Critical fix:
            # recovery_unavailable gets a short retry cooldown.
            # It must NOT receive the 24-hour success lock.
            await _handle_recovery_attempt_result(
                breakdown_key,
                recovery_event,
            )

            await _broadcast_recovery_event(recovery_event)

        except Exception as exc:
            # A parallel request may have created the plan between
            # the initial lookup and execution. Treat that as success.
            try:
                existing = await get_active_recovery(
                    db,
                    payload.vehicle_id,
                )
            except Exception:
                existing = None
                logger.exception(
                    "Could not check active recovery after failure for %s",
                    payload.vehicle_id,
                )

            if existing:
                recovery_event = {
                    "type": "recovery_status_updated",
                    **existing,
                    "automatic": True,
                    "already_exists": True,
                    "message": (
                        f"Recovery is already assigned to "
                        f"{existing['helper_vehicle_id']}."
                    ),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

                await _set_recovery_lock(
                    breakdown_key,
                    "recovery_created",
                    RECOVERY_SUCCESS_LOCK_SECONDS,
                )

            else:
                logger.exception(
                    "Automatic recovery failed for %s",
                    payload.vehicle_id,
                )

                recovery_event = {
                    "type": "recovery_failed",
                    "delayed_vehicle_id": payload.vehicle_id,
                    "message": (
                        "Breakdown was recorded, but automatic "
                        f"recovery failed: {type(exc).__name__}: {exc}"
                    ),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

                # Retry later rather than permanently suppressing
                # automatic recovery for this incident.
                await _set_recovery_lock(
                    breakdown_key,
                    "retry_after_failure",
                    RECOVERY_RETRY_SECONDS,
                )

            await _broadcast_recovery_event(recovery_event)

    # ------------------------------------------------------
    # GPS-DRIVEN RECOVERY LIFECYCLE
    # ------------------------------------------------------

    # This runs for every accepted telemetry event. Each helper's
    # telemetry advances only the recovery plans associated with it.
    try:
        await process_recovery_telemetry(
            db,
            payload.vehicle_id,
            payload.latitude,
            payload.longitude,
        )

    except Exception:
        logger.exception(
            "Recovery lifecycle processing failed for telemetry from %s",
            payload.vehicle_id,
        )

    return {
        "success": True,
        "accepted": True,
        "event_id": payload.event_id,
        "vehicle_id": payload.vehicle_id,
        "recorded_at": recorded_at.isoformat(),
        "status": vehicle.status,
        "movement_classification": classification,
        "events_generated": len(events),
        "events": events,
        "automatic_recovery": recovery_event,
    }
