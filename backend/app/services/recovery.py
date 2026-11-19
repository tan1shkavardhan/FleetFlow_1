
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Delivery,
    Package,
    RecoveryPlan,
    RecoveryStatusHistory,
    Telemetry,
    Vehicle,
)
from app.redis_client import redis_client
from app.config import settings
from app.services.geo import distance_km
from app.services.packing import pack_packages
from app.services.routing import optimize_route


MAX_ADDITIONAL_ROUTE_KM = 5.0
MAX_ROUTE_DETOUR_RATIO = 0.35

ACTIVE_PACKAGE_STATUSES = (
    "PENDING",
    "IN_TRANSIT",
    "REASSIGNED",
)

BUSY_RECOVERY_STATUSES = (
    "ASSIGNED",
    "EN_ROUTE_TO_INCIDENT",
    "ARRIVED_AT_INCIDENT",
    "HANDOVER_IN_PROGRESS",
    "HANDOVER_COMPLETED",
    "IN_TRANSIT_TO_DESTINATION",
)


def _delivery_dict(delivery: Delivery) -> dict:
    return {
        "id": delivery.id,
        "customer_name": delivery.customer_name,
        "latitude": delivery.latitude,
        "longitude": delivery.longitude,
        "priority": delivery.priority,
        "service_minutes": delivery.service_minutes,
        "time_window_start": delivery.time_window_start,
        "time_window_end": delivery.time_window_end,
    }


def _package_dict(package: Package) -> dict:
    return {
        "id": package.id,
        "delivery_id": package.delivery_id,
        "length_cm": package.length_cm,
        "width_cm": package.width_cm,
        "height_cm": package.height_cm,
        "weight_kg": package.weight_kg,
        "allowed_rotations": package.allowed_rotations,
    }


def _delivery_order(deliveries: list[Delivery]) -> dict[str, int]:
    ordered = sorted(
        deliveries,
        key=lambda d: (
            -d.priority,
            d.time_window_end
            or datetime.max.replace(tzinfo=timezone.utc),
            d.id,
        ),
    )
    return {
        delivery.id: index
        for index, delivery in enumerate(ordered)
    }


async def latest_position(
    db: AsyncSession,
    vehicle_id: str,
) -> tuple[float, float] | None:
    row = (
        await db.execute(
            select(Telemetry)
            .where(Telemetry.vehicle_id == vehicle_id)
            .order_by(
                Telemetry.recorded_at.desc(),
                Telemetry.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    if row:
        return row.latitude, row.longitude

    try:
        position = await redis_client.geopos(
            settings.vehicle_geo_key,
            vehicle_id,
        )

        if position and position[0]:
            longitude, latitude = position[0]
            return float(latitude), float(longitude)

    except Exception:
        pass

    return None


async def get_active_recovery(
    db: AsyncSession,
    delayed_vehicle_id: str,
) -> dict | None:
    """Return the existing non-terminal recovery for one incident vehicle."""

    result = await db.execute(
        select(RecoveryPlan)
        .where(
            RecoveryPlan.delayed_vehicle_id == delayed_vehicle_id,
            RecoveryPlan.status.in_(BUSY_RECOVERY_STATUSES),
        )
        .order_by(RecoveryPlan.created_at.desc())
        .limit(1)
    )

    plan = result.scalar_one_or_none()

    if plan is None:
        return None

    plan_data = plan.plan_json or {}

    return {
        "recovery_plan_id": plan.id,
        "delayed_vehicle_id": plan.delayed_vehicle_id,
        "helper_vehicle_id": plan.helper_vehicle_id,
        "status": plan.status,
        "package_ids": plan_data.get("package_ids", []),
        "delivery_ids": plan_data.get("delivery_ids", []),
        "cargo_transfer_status": plan_data.get(
            "cargo_transfer_status",
            "PENDING",
        ),
    }


async def _get_vehicle_deliveries(
    db: AsyncSession,
    vehicle_id: str,
) -> list[Delivery]:
    result = await db.execute(
        select(Delivery).where(
            Delivery.assigned_vehicle_id == vehicle_id,
            Delivery.status.in_(ACTIVE_PACKAGE_STATUSES),
        )
    )

    return result.scalars().all()


async def _busy_helper_ids(
    db: AsyncSession,
) -> set[str]:
    """Return helpers reserved by non-terminal recovery plans."""

    result = await db.execute(
        select(RecoveryPlan.helper_vehicle_id).where(
            RecoveryPlan.status.in_(BUSY_RECOVERY_STATUSES),
            RecoveryPlan.helper_vehicle_id.is_not(None),
        )
    )

    return {
        row[0]
        for row in result.all()
    }


async def analyze_recovery(
    db: AsyncSession,
    delayed_vehicle_id: str,
    package_ids: list[str],
    delivery_ids: list[str],
    radius_km: float,
    max_candidates: int,
    origin_latitude: float | None = None,
    origin_longitude: float | None = None,
    apply_accessibility: bool = True,
) -> dict:

    delayed = await db.get(
        Vehicle,
        delayed_vehicle_id,
    )

    if not delayed:
        raise ValueError(
            f"Delayed vehicle '{delayed_vehicle_id}' was not found."
        )

    if origin_latitude is not None and origin_longitude is not None:
        origin = (
            origin_latitude,
            origin_longitude,
        )
    else:
        origin = await latest_position(
            db,
            delayed_vehicle_id,
        )

    if not origin:
        raise ValueError(
            "No current position is available for the delayed vehicle."
        )

    # Resolve only the active packages belonging to this incident.
    if package_ids:
        result = await db.execute(
            select(Package).where(
                Package.id.in_(package_ids),
                Package.status.in_(ACTIVE_PACKAGE_STATUSES),
            )
        )
        packages = result.scalars().all()

    elif delivery_ids:
        result = await db.execute(
            select(Package).where(
                Package.delivery_id.in_(delivery_ids),
                Package.status.in_(ACTIVE_PACKAGE_STATUSES),
            )
        )
        packages = result.scalars().all()

    else:
        result = await db.execute(
            select(Package).where(
                Package.assigned_vehicle_id == delayed_vehicle_id,
                Package.status.in_(ACTIVE_PACKAGE_STATUSES),
            )
        )
        packages = result.scalars().all()

    found_ids = {
        package.id
        for package in packages
    }

    missing_ids = sorted(
        set(package_ids) - found_ids
    )

    if missing_ids:
        raise ValueError(
            "Unknown or inactive package IDs: "
            + ", ".join(missing_ids)
        )

    if not packages:
        raise ValueError(
            "No undelivered packages were found for this vehicle."
        )

    incorrectly_assigned = [
        package.id
        for package in packages
        if package.assigned_vehicle_id not in (
            None,
            delayed_vehicle_id,
        )
    ]

    if incorrectly_assigned:
        raise ValueError(
            "Packages are not assigned to the delayed vehicle: "
            + ", ".join(incorrectly_assigned)
        )

    # Delivery IDs must match the packages selected for this incident.
    transfer_delivery_ids = {
        package.delivery_id
        for package in packages
        if package.delivery_id
    }

    if delivery_ids and set(delivery_ids) != transfer_delivery_ids:
        missing_for_packages = sorted(
            transfer_delivery_ids - set(delivery_ids)
        )

        unrelated_deliveries = sorted(
            set(delivery_ids) - transfer_delivery_ids
        )

        details = []

        if missing_for_packages:
            details.append(
                "missing package-linked deliveries: "
                + ", ".join(missing_for_packages)
            )

        if unrelated_deliveries:
            details.append(
                "deliveries not linked to selected packages: "
                + ", ".join(unrelated_deliveries)
            )

        raise ValueError(
            "Delivery selection does not match package selection ("
            + "; ".join(details)
            + ")."
        )

    transfer_deliveries = []

    if transfer_delivery_ids:
        result = await db.execute(
            select(Delivery).where(
                Delivery.id.in_(transfer_delivery_ids)
            )
        )

        transfer_deliveries = result.scalars().all()

    transfer_delivery_by_id = {
        delivery.id: delivery
        for delivery in transfer_deliveries
    }

    busy_vehicle_ids = await _busy_helper_ids(db)

    candidate_result = await db.execute(
        select(Vehicle).where(
            Vehicle.active.is_(True),
            Vehicle.id != delayed_vehicle_id,
            Vehicle.status.not_in(
                [
                    "BREAKDOWN",
                    "INACTIVE",
                    "UNAVAILABLE",
                    "OFFLINE",
                    "STOPPED",
                ]
            ),
        )
    )

    vehicles = candidate_result.scalars().all()

    evaluated = []

    for vehicle in vehicles:

        # A helper reserved by another active incident is unavailable.
        if vehicle.id in busy_vehicle_ids:
            continue

        helper_position = await latest_position(
            db,
            vehicle.id,
        )

        if not helper_position:
            continue

        incident_distance = distance_km(
            origin[0],
            origin[1],
            helper_position[0],
            helper_position[1],
        )

        if incident_distance > radius_km:
            continue

        # Existing cargo already occupying this helper.
        existing_package_result = await db.execute(
            select(Package).where(
                Package.assigned_vehicle_id == vehicle.id,
                Package.status.in_(ACTIVE_PACKAGE_STATUSES),
            )
        )

        existing_packages = [
            package
            for package in existing_package_result.scalars().all()
            if package.id not in found_ids
        ]

        existing_weight = sum(
            package.weight_kg
            for package in existing_packages
        )

        existing_deliveries = await _get_vehicle_deliveries(
            db,
            vehicle.id,
        )

        existing_delivery_by_id = {
            delivery.id: delivery
            for delivery in existing_deliveries
        }

        existing_cargo_delivery_ids = {
            package.delivery_id
            for package in existing_packages
            if package.delivery_id
        }

        missing_existing_delivery_ids = (
            existing_cargo_delivery_ids
            - set(existing_delivery_by_id)
        )

        if missing_existing_delivery_ids:
            result = await db.execute(
                select(Delivery).where(
                    Delivery.id.in_(
                        missing_existing_delivery_ids
                    )
                )
            )

            for delivery in result.scalars().all():
                existing_delivery_by_id[delivery.id] = delivery

        # Test packing with both existing and transferred cargo.
        all_cargo = existing_packages + packages

        all_delivery_by_id = dict(existing_delivery_by_id)
        all_delivery_by_id.update(transfer_delivery_by_id)

        all_delivery_ids = {
            package.delivery_id
            for package in all_cargo
            if package.delivery_id
        }

        missing_delivery_ids = (
            all_delivery_ids - set(all_delivery_by_id)
        )

        if missing_delivery_ids:
            result = await db.execute(
                select(Delivery).where(
                    Delivery.id.in_(missing_delivery_ids)
                )
            )

            for delivery in result.scalars().all():
                all_delivery_by_id[delivery.id] = delivery

        all_deliveries = list(
            all_delivery_by_id.values()
        )

        packing = pack_packages(
            {
                "id": vehicle.id,
                "cargo_length_cm": vehicle.cargo_length_cm,
                "cargo_width_cm": vehicle.cargo_width_cm,
                "cargo_height_cm": vehicle.cargo_height_cm,
                "capacity_kg": vehicle.capacity_kg,
                "max_payload_kg": vehicle.max_payload_kg,
            },
            [
                _package_dict(package)
                for package in all_cargo
            ],
            _delivery_order(all_deliveries),
            apply_accessibility,
        )

        # Baseline: helper current position -> its existing destinations.
        baseline_route = optimize_route(
            helper_position,
            [
                _delivery_dict(delivery)
                for delivery in existing_deliveries
            ],
        )

        # Proposed: incident pickup -> all existing and transferred stops.
        proposed_route = optimize_route(
            origin,
            [
                _delivery_dict(delivery)
                for delivery in all_deliveries
            ],
        )

        baseline_distance = float(
            baseline_route.get("total_distance_km", 0)
        )

        proposed_distance = (
            incident_distance
            + float(
                proposed_route.get("total_distance_km", 0)
            )
        )

        additional_distance = max(
            0.0,
            proposed_distance - baseline_distance,
        )

        has_existing_route = baseline_distance > 0.001

        if has_existing_route:
            detour_ratio = (
                additional_distance / baseline_distance
            )

            route_compatible = (
                additional_distance <= MAX_ADDITIONAL_ROUTE_KM
                and detour_ratio <= MAX_ROUTE_DETOUR_RATIO
            )

        else:
            # No existing route means there is no route to divert.
            # The incident radius and packing constraints still apply.
            detour_ratio = 0.0
            route_compatible = True

        feasible = (
            packing["feasible"]
            and route_compatible
        )

        rejection_reasons = []

        if not packing["feasible"]:
            rejection_reasons.append(
                packing.get(
                    "failure_reason",
                    "Cargo packing is infeasible.",
                )
            )

        if not route_compatible:
            rejection_reasons.append(
                "Route detour exceeds the configured limit "
                f"({additional_distance:.2f} km additional, "
                f"{detour_ratio * 100:.1f}% detour)."
            )

        evaluated.append(
            {
                "vehicle_id": vehicle.id,
                "vehicle_type": vehicle.vehicle_type,
                "distance_km": round(
                    incident_distance,
                    3,
                ),
                "position": {
                    "latitude": helper_position[0],
                    "longitude": helper_position[1],
                },
                "existing_payload_kg": round(
                    existing_weight,
                    2,
                ),
                "available_payload_kg": round(
                    max(
                        0,
                        vehicle.max_payload_kg - existing_weight,
                    ),
                    2,
                ),
                "existing_package_ids": [
                    package.id
                    for package in existing_packages
                ],
                "transfer_package_ids": [
                    package.id
                    for package in packages
                ],
                "packing": packing,
                "route": {
                    "has_existing_route": has_existing_route,
                    "baseline_distance_km": round(
                        baseline_distance,
                        3,
                    ),
                    "proposed_distance_km": round(
                        proposed_distance,
                        3,
                    ),
                    "recovery_journey_distance_km": round(
                        proposed_distance,
                        3,
                    ),
                    "additional_distance_km": round(
                        additional_distance,
                        3,
                    ),
                    "detour_percentage": round(
                        detour_ratio * 100,
                        2,
                    ),
                    "max_additional_distance_km": (
                        MAX_ADDITIONAL_ROUTE_KM
                    ),
                    "max_detour_percentage": (
                        MAX_ROUTE_DETOUR_RATIO * 100
                    ),
                    "detour_limit_applied": has_existing_route,
                    "compatible": route_compatible,
                    "existing_route": baseline_route,
                    "proposed_route_after_pickup": proposed_route,
                },
                "route_compatibility": (
                    "COMPATIBLE"
                    if route_compatible
                    else "EXCESSIVE_DETOUR"
                ),
                "feasible": feasible,
                "recommendation": (
                    "SUITABLE"
                    if feasible
                    else "UNSUITABLE"
                ),
                "rejection_reasons": rejection_reasons,
                "rejection_reason": (
                    "; ".join(rejection_reasons)
                    if rejection_reasons
                    else None
                ),
            }
        )

    evaluated.sort(
        key=lambda candidate: (
            not candidate["feasible"],
            candidate["route"]["additional_distance_km"],
            candidate["distance_km"],
        )
    )

    feasible_candidates = [
        candidate
        for candidate in evaluated
        if candidate["feasible"]
    ]

    return {
        "delayed_vehicle_id": delayed_vehicle_id,
        "origin": {
            "latitude": origin[0],
            "longitude": origin[1],
        },
        "radius_km": radius_km,
        "package_ids": [
            package.id
            for package in packages
        ],
        "delivery_ids": sorted(transfer_delivery_ids),
        "candidate_count": len(evaluated),
        "recommended_candidate": (
            feasible_candidates[0]
            if feasible_candidates
            else None
        ),
        "candidates": evaluated[
            :max(1, max_candidates)
        ],
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "route_constraints": {
            "max_additional_distance_km": (
                MAX_ADDITIONAL_ROUTE_KM
            ),
            "max_detour_percentage": (
                MAX_ROUTE_DETOUR_RATIO * 100
            ),
        },
        "notes": [
            "Cargo feasibility uses the existing deterministic 3D packing heuristic.",
            "Route distances use great-circle estimates, not road-network navigation.",
            "The helper route includes the breakdown pickup before its delivery destinations.",
            "Detour limits apply only when the helper already has an existing delivery route.",
            "Helpers reserved by active recovery plans are excluded from candidate selection.",
        ],
    }


async def execute_recovery(
    db: AsyncSession,
    delayed_vehicle_id: str,
    helper_vehicle_id: str,
    package_ids: list[str],
    delivery_ids: list[str],
    plan: dict,
) -> dict:

    # Lock both vehicles in deterministic order. If two incidents compete
    # for the same helper, only one transaction can reserve it at a time.
    lock_ids = sorted({
        delayed_vehicle_id,
        helper_vehicle_id,
    })

    locked_vehicles = (
        await db.execute(
            select(Vehicle)
            .where(Vehicle.id.in_(lock_ids))
            .order_by(Vehicle.id)
            .with_for_update()
        )
    ).scalars().all()

    vehicles_by_id = {
        vehicle.id: vehicle
        for vehicle in locked_vehicles
    }

    delayed_vehicle = vehicles_by_id.get(
        delayed_vehicle_id
    )

    helper = vehicles_by_id.get(
        helper_vehicle_id
    )

    if not delayed_vehicle:
        raise ValueError(
            "Incident vehicle was not found."
        )

    if delayed_vehicle_id == helper_vehicle_id:
        raise ValueError(
            "A vehicle cannot recover itself."
        )

    # One active plan per incident vehicle.
    active_incident = (
        await db.execute(
            select(
                RecoveryPlan.id,
                RecoveryPlan.helper_vehicle_id,
                RecoveryPlan.status,
            )
            .where(
                RecoveryPlan.delayed_vehicle_id == delayed_vehicle_id,
                RecoveryPlan.status.in_(BUSY_RECOVERY_STATUSES),
            )
            .limit(1)
        )
    ).first()

    if active_incident:
        raise ValueError(
            f"An active recovery already exists for {delayed_vehicle_id} "
            f"(plan {active_incident.id}, helper "
            f"{active_incident.helper_vehicle_id}, "
            f"status {active_incident.status})."
        )

    if (
        not helper
        or not helper.active
        or helper.status in {
            "BREAKDOWN",
            "INACTIVE",
            "UNAVAILABLE",
            "OFFLINE",
            "STOPPED",
        }
    ):
        raise ValueError(
            "Helper vehicle is missing, inactive, unavailable, or broken down."
        )

    # Confirm that the selected packages still exist and are active.
    chosen = (
        await db.execute(
            select(Package)
            .where(
                Package.id.in_(package_ids),
                Package.status.in_(ACTIVE_PACKAGE_STATUSES),
            )
            .with_for_update()
        )
    ).scalars().all() if package_ids else []

    if package_ids and {
        package.id for package in chosen
    } != set(package_ids):
        raise ValueError(
            "One or more packages are missing or no longer active."
        )

    if not chosen:
        raise ValueError(
            "There are no active packages to transfer."
        )

    if any(
        package.assigned_vehicle_id
        not in (None, delayed_vehicle_id)
        for package in chosen
    ):
        raise ValueError(
            "One or more packages are no longer assigned to the incident vehicle."
        )

    package_delivery_ids = {
        package.delivery_id
        for package in chosen
        if package.delivery_id
    }

    requested_delivery_ids = set(delivery_ids)

    if requested_delivery_ids != package_delivery_ids:
        raise ValueError(
            "Package and delivery selections do not match."
        )

    delivery_rows = (
        await db.execute(
            select(Delivery)
            .where(Delivery.id.in_(delivery_ids))
            .with_for_update()
        )
    ).scalars().all() if delivery_ids else []

    if delivery_ids and {
        delivery.id for delivery in delivery_rows
    } != set(delivery_ids):
        raise ValueError(
            "One or more delivery IDs were not found."
        )

    # The helper must not already be reserved by another active plan.
    busy_result = await db.execute(
        select(RecoveryPlan.id).where(
            RecoveryPlan.helper_vehicle_id == helper_vehicle_id,
            RecoveryPlan.status.in_(BUSY_RECOVERY_STATUSES),
        )
    )

    if busy_result.first():
        raise ValueError(
            "The selected helper is already handling another recovery."
        )

    candidate = next(
        (
            item
            for item in plan.get("candidates", [])
            if item.get("vehicle_id") == helper_vehicle_id
        ),
        None,
    )

    if not candidate or not candidate.get("feasible"):
        raise ValueError(
            "The selected helper was not verified as feasible."
        )

    if not candidate.get("route", {}).get("compatible"):
        raise ValueError(
            "The selected helper does not satisfy route compatibility limits."
        )

    helper_position = await latest_position(
        db,
        helper_vehicle_id,
    )

    origin = plan.get("origin") or {}

    if (
        not helper_position
        or "latitude" not in origin
        or "longitude" not in origin
    ):
        raise ValueError(
            "Current helper or breakdown location is unavailable. Analyze again."
        )

    current_distance = distance_km(
        float(origin["latitude"]),
        float(origin["longitude"]),
        helper_position[0],
        helper_position[1],
    )

    if current_distance > float(
        plan.get("radius_km", 20)
    ):
        raise ValueError(
            "The selected helper is no longer within the recovery radius."
        )

    # Re-read helper cargo immediately before creating the plan.
    existing_result = await db.execute(
        select(Package).where(
            Package.assigned_vehicle_id == helper_vehicle_id,
            Package.status.in_(ACTIVE_PACKAGE_STATUSES),
        )
    )

    existing = [
        package
        for package in existing_result.scalars().all()
        if package.id not in set(package_ids)
    ]

    all_cargo = existing + chosen

    delivery_ids_for_order = {
        package.delivery_id
        for package in all_cargo
        if package.delivery_id
    }

    current_deliveries = (
        await db.execute(
            select(Delivery).where(
                Delivery.id.in_(delivery_ids_for_order)
            )
        )
    ).scalars().all() if delivery_ids_for_order else []

    current_packing = pack_packages(
        {
            "id": helper.id,
            "cargo_length_cm": helper.cargo_length_cm,
            "cargo_width_cm": helper.cargo_width_cm,
            "cargo_height_cm": helper.cargo_height_cm,
            "max_payload_kg": helper.max_payload_kg,
        },
        [
            _package_dict(package)
            for package in all_cargo
        ],
        _delivery_order(current_deliveries),
        True,
    )

    if not current_packing["feasible"]:
        raise ValueError(
            "The helper's current cargo no longer fits: "
            + str(current_packing.get("failure_reason"))
        )

    # Re-run the full analysis against current positions, cargo and
    # helper reservations. This prevents stale candidate data being used.
    current_analysis = await analyze_recovery(
        db=db,
        delayed_vehicle_id=delayed_vehicle_id,
        package_ids=package_ids,
        delivery_ids=delivery_ids,
        radius_km=float(plan.get("radius_km", 20)),
        max_candidates=200,
        origin_latitude=float(origin["latitude"]),
        origin_longitude=float(origin["longitude"]),
    )

    current_candidate = next(
        (
            item
            for item in current_analysis["candidates"]
            if item["vehicle_id"] == helper_vehicle_id
        ),
        None,
    )

    if (
        not current_candidate
        or not current_candidate["feasible"]
        or not current_candidate["route"]["compatible"]
    ):
        raise ValueError(
            "The helper is no longer feasible after revalidation."
        )

    # Create a separate recovery plan for this incident.
    # Cargo ownership stays with the incident vehicle until handover.
    plan_id = f"recovery_{uuid4().hex[:12]}"
    assigned_at = datetime.now(timezone.utc).isoformat()

    execution_plan = {
        **plan,
        "package_ids": package_ids,
        "delivery_ids": delivery_ids,
        "selected_candidate": current_candidate,
        "cargo_transfer_status": "PENDING",
        "cargo_transfer_at": None,
        "execution_revalidation": {
            "feasible": True,
            "packing": current_packing,
            "checked_at": assigned_at,
        },
    }

    record = RecoveryPlan(
        id=plan_id,
        delayed_vehicle_id=delayed_vehicle_id,
        helper_vehicle_id=helper_vehicle_id,
        status="ASSIGNED",
        radius_km=float(plan.get("radius_km", 20)),
        plan_json=execution_plan,
    )

    db.add(record)

    db.add(
        RecoveryStatusHistory(
            id=f"rsh_{uuid4().hex[:12]}",
            recovery_plan_id=plan_id,
            previous_status=None,
            new_status="ASSIGNED",
            notes=(
                "Recovery assignment executed after feasibility "
                "and helper availability revalidation."
            ),
            simulated=True,
        )
    )

    await db.commit()

    return {
        "recovery_plan_id": plan_id,
        "status": "ASSIGNED",
        "delayed_vehicle_id": delayed_vehicle_id,
        "helper_vehicle_id": helper_vehicle_id,
        "reassigned_package_ids": [],
        "reassigned_delivery_ids": [],
        "pending_transfer_package_ids": [
            package.id
            for package in chosen
        ],
        "pending_transfer_delivery_ids": [
            delivery.id
            for delivery in delivery_rows
        ],
        "cargo_transfer_status": "PENDING",
        "route_compatibility": current_candidate["route"],
    }
