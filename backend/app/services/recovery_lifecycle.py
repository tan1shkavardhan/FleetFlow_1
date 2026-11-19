from datetime import datetime, timezone
from uuid import uuid4
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Delivery,
    Package,
    RecoveryPlan,
    RecoveryStatusHistory,
    Vehicle,
)
from app.services.geo import distance_km
from app.services.websocket_manager import manager

log = logging.getLogger(__name__)

ARRIVAL_RADIUS_KM = 0.08
HANDOVER_SECONDS = 5

ACTIVE = (
    "ASSIGNED",
    "EN_ROUTE_TO_INCIDENT",
    "ARRIVED_AT_INCIDENT",
    "HANDOVER_IN_PROGRESS",
    "HANDOVER_COMPLETED",
    "IN_TRANSIT_TO_DESTINATION",
)


async def _transition(db, plan, target, note, data):
    old = plan.status

    plan.status = target
    plan.plan_json = data

    db.add(
        RecoveryStatusHistory(
            id=f"rsh_{uuid4().hex[:12]}",
            recovery_plan_id=plan.id,
            previous_status=old,
            new_status=target,
            notes=note,
            simulated=False,
        )
    )

    await db.commit()

    await manager.broadcast({
        "type": "recovery_status_updated",
        "recovery_plan_id": plan.id,
        "delayed_vehicle_id": plan.delayed_vehicle_id,
        "helper_vehicle_id": plan.helper_vehicle_id,
        "status": target,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": note,
    })


async def process_recovery_telemetry(
    db: AsyncSession,
    vehicle_id: str,
    latitude: float,
    longitude: float
) -> None:
    """
    Advance recovery using helper GPS coordinates and persisted
    handover timing.
    """

    rows = (
        await db.execute(
            select(RecoveryPlan)
            .where(
                RecoveryPlan.status.in_(ACTIVE),
                RecoveryPlan.helper_vehicle_id == vehicle_id,
            )
            .with_for_update()
        )
    ).scalars().all()

    now = datetime.now(timezone.utc)

    for plan in rows:
        data = dict(plan.plan_json or {})
        origin = data.get("origin") or {}

        if plan.status in (
            "ASSIGNED",
            "EN_ROUTE_TO_INCIDENT"
        ):
            target_lat = origin.get("latitude")
            target_lon = origin.get("longitude")

            if plan.status == "ASSIGNED":
                await _transition(
                    db,
                    plan,
                    "EN_ROUTE_TO_INCIDENT",
                    "Helper GPS telemetry received; helper is en route to incident.",
                    data
                )
                continue

            if (
                target_lat is not None
                and target_lon is not None
                and distance_km(
                    latitude,
                    longitude,
                    float(target_lat),
                    float(target_lon)
                ) <= ARRIVAL_RADIUS_KM
            ):
                await _transition(
                    db,
                    plan,
                    "ARRIVED_AT_INCIDENT",
                    "Helper arrived within the incident geofence.",
                    data
                )

                data["handover_started_at"] = now.isoformat()

                await _transition(
                    db,
                    plan,
                    "HANDOVER_IN_PROGRESS",
                    "Cargo handover started at incident location.",
                    data
                )

            continue

        if plan.status == "HANDOVER_IN_PROGRESS":
            started = data.get("handover_started_at")

            if not started:
                data["handover_started_at"] = now.isoformat()
                plan.plan_json = data
                await db.commit()
                continue

            try:
                start_dt = datetime.fromisoformat(started)

                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(
                        tzinfo=timezone.utc
                    )

            except ValueError:
                start_dt = now

            if (
                now - start_dt
            ).total_seconds() < HANDOVER_SECONDS:
                continue

            package_ids = data.get("package_ids", [])
            delivery_ids = data.get("delivery_ids", [])

            packages = (
                await db.execute(
                    select(Package)
                    .where(Package.id.in_(package_ids))
                    .with_for_update()
                )
            ).scalars().all() if package_ids else []

            transferred_deliveries = (
                await db.execute(
                    select(Delivery)
                    .where(Delivery.id.in_(delivery_ids))
                    .with_for_update()
                )
            ).scalars().all() if delivery_ids else []

            # Transfer only the packages belonging to this recovery.
            for package in packages:
                package.assigned_vehicle_id = plan.helper_vehicle_id

                if package.status in (
                    "PENDING",
                    "IN_TRANSIT",
                    "REASSIGNED"
                ):
                    package.status = "IN_TRANSIT"

            for delivery in transferred_deliveries:
                delivery.assigned_vehicle_id = plan.helper_vehicle_id

                if delivery.status in (
                    "PENDING",
                    "REASSIGNED"
                ):
                    delivery.status = "IN_TRANSIT"

            # Include the helper's own outstanding deliveries.
            # This keeps the actual lifecycle consistent with the
            # recovery analyzer's existing-route feasibility check.
            helper_result = await db.execute(
                select(Delivery).where(
                    Delivery.assigned_vehicle_id == plan.helper_vehicle_id,
                    Delivery.status.in_([
                        "PENDING",
                        "REASSIGNED",
                        "IN_TRANSIT",
                    ]),
                )
            )

            helper_deliveries = helper_result.scalars().all()

            all_deliveries = {
                delivery.id: delivery
                for delivery in helper_deliveries
            }

            for delivery in transferred_deliveries:
                if delivery.status != "DELIVERED":
                    all_deliveries[delivery.id] = delivery

            destinations = [
                {
                    "delivery_id": delivery.id,
                    "latitude": delivery.latitude,
                    "longitude": delivery.longitude,
                    "status": delivery.status,
                }
                for delivery in all_deliveries.values()
            ]

            data["cargo_transfer_status"] = "COMPLETED"
            data["cargo_transfer_at"] = now.isoformat()
            data["destinations"] = destinations
            data["destination_index"] = 0

            await db.commit()

            await _transition(
                db,
                plan,
                "HANDOVER_COMPLETED",
                "Cargo ownership transferred to helper vehicle.",
                data
            )

            if destinations:
                await _transition(
                    db,
                    plan,
                    "IN_TRANSIT_TO_DESTINATION",
                    "Helper dispatched to the first delivery destination.",
                    data
                )
            else:
                await _transition(
                    db,
                    plan,
                    "COMPLETED",
                    "Recovery completed; no delivery stops were required.",
                    data
                )

                incident = await db.get(
                    Vehicle,
                    plan.delayed_vehicle_id
                )

                if incident:
                    incident.status = "RECOVERED"
                    await db.commit()

            continue

        if plan.status == "HANDOVER_COMPLETED":
            destinations = data.get("destinations", [])

            if destinations:
                await _transition(
                    db,
                    plan,
                    "IN_TRANSIT_TO_DESTINATION",
                    "Helper is travelling to delivery destinations.",
                    data
                )
            else:
                await _transition(
                    db,
                    plan,
                    "COMPLETED",
                    "Recovery completed.",
                    data
                )

            continue

        if plan.status == "IN_TRANSIT_TO_DESTINATION":
            destinations = data.get("destinations", [])

            index = int(
                data.get("destination_index", 0)
            )

            if index >= len(destinations):
                await _transition(
                    db,
                    plan,
                    "COMPLETED",
                    "All transferred deliveries completed.",
                    data
                )

                incident = await db.get(
                    Vehicle,
                    plan.delayed_vehicle_id
                )

                if incident:
                    incident.status = "RECOVERED"
                    await db.commit()

                continue

            destination = destinations[index]

            distance = distance_km(
                latitude,
                longitude,
                float(destination["latitude"]),
                float(destination["longitude"])
            )

            if distance <= ARRIVAL_RADIUS_KM:
                delivery_id = destination["delivery_id"]

                delivery = await db.get(
                    Delivery,
                    delivery_id
                )

                if delivery:
                    delivery.status = "DELIVERED"

                packages_result = await db.execute(
                    select(Package).where(
                        Package.delivery_id == delivery_id
                    )
                )

                for package in packages_result.scalars().all():
                    package.status = "DELIVERED"

                data["destination_index"] = index + 1

                await db.commit()

                await manager.broadcast({
                    "type": "recovery_delivery_completed",
                    "recovery_plan_id": plan.id,
                    "delivery_id": delivery_id,
                    "completed": index + 1,
                    "total": len(destinations),
                    "timestamp": now.isoformat(),
                })

                if index + 1 >= len(destinations):
                    await _transition(
                        db,
                        plan,
                        "COMPLETED",
                        "All transferred deliveries completed.",
                        data
                    )

                    incident = await db.get(
                        Vehicle,
                        plan.delayed_vehicle_id
                    )

                    if incident:
                        incident.status = "RECOVERED"
                        await db.commit()

                else:
                    await _transition(
                        db,
                        plan,
                        "IN_TRANSIT_TO_DESTINATION",
                        (
                            f"Delivery {delivery_id} completed; "
                            "proceeding to next stop."
                        ),
                        data
                    )