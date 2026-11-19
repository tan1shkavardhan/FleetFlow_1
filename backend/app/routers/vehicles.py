from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Delivery, Package, Telemetry, Vehicle
from app.schemas import VehicleCreate, VehicleOut
from app.redis_client import redis_client
from app.config import settings
from app.services.geo import distance_km

router = APIRouter(prefix="/vehicles", tags=["vehicles"])


async def _vehicle_with_latest_telemetry(
    vehicle: Vehicle,
    db: AsyncSession
) -> dict:
    """Merge a PostgreSQL vehicle record with its latest live telemetry."""

    data = VehicleOut.model_validate(vehicle).model_dump()
    telemetry_data = None

    try:
        raw = await redis_client.hgetall(
            f"vehicle:latest:{vehicle.id}"
        )

        if raw and "latitude" in raw and "longitude" in raw:
            telemetry_data = {
                "latitude": float(raw["latitude"]),
                "longitude": float(raw["longitude"]),
                "speed": (
                    float(raw["speed"])
                    if raw.get("speed") not in (None, "")
                    else None
                ),
                "heading": (
                    float(raw["heading"])
                    if raw.get("heading") not in (None, "")
                    else None
                ),
                "last_update": raw.get("timestamp"),
            }

    except Exception:
        telemetry_data = None

    if telemetry_data is None:
        result = await db.execute(
            select(Telemetry)
            .where(Telemetry.vehicle_id == vehicle.id)
            .order_by(
                Telemetry.recorded_at.desc(),
                Telemetry.id.desc()
            )
            .limit(1)
        )

        latest = result.scalars().first()

        if latest is not None:
            telemetry_data = {
                "latitude": latest.latitude,
                "longitude": latest.longitude,
                "speed": latest.speed,
                "heading": latest.heading,
                "last_update": latest.recorded_at.isoformat(),
            }

    if telemetry_data is not None:
        data.update(telemetry_data)
        data["timestamp"] = telemetry_data.get("last_update")

    return data


@router.post(
    "/",
    response_model=VehicleOut,
    status_code=status.HTTP_201_CREATED
)
async def register_vehicle(
    payload: VehicleCreate,
    db: AsyncSession = Depends(get_db)
):
    if await db.get(Vehicle, payload.id):
        raise HTTPException(
            409,
            detail=f"Vehicle '{payload.id}' already exists"
        )

    if payload.capacity_kg < payload.max_payload_kg:
        raise HTTPException(
            422,
            detail="max_payload_kg cannot exceed capacity_kg"
        )

    obj = Vehicle(**payload.model_dump())

    db.add(obj)
    await db.commit()
    await db.refresh(obj)

    return await _vehicle_with_latest_telemetry(obj, db)


@router.get("/", response_model=list[VehicleOut])
async def list_vehicles(
    active_only: bool = True,
    db: AsyncSession = Depends(get_db)
):
    stmt = select(Vehicle).order_by(Vehicle.id)

    if active_only:
        stmt = stmt.where(Vehicle.active.is_(True))

    vehicles = (
        await db.execute(stmt)
    ).scalars().all()

    return [
        await _vehicle_with_latest_telemetry(vehicle, db)
        for vehicle in vehicles
    ]


@router.get("/nearby")
async def nearby_vehicles(
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(20, gt=0, le=500),
    exclude_vehicle_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    results = []
    redis_error = None

    try:
        raw = await redis_client.georadius(
            settings.vehicle_geo_key,
            longitude,
            latitude,
            radius_km,
            unit="km",
            withdist=True,
            withcoord=True,
            sort="ASC",
        )

        for item in raw:
            vid = item[0]

            if exclude_vehicle_id and vid == exclude_vehicle_id:
                continue

            results.append({
                "vehicle_id": vid,
                "distance_km": round(float(item[1]), 3),
                "longitude": float(item[2][0]),
                "latitude": float(item[2][1]),
                "source": "redis_geo",
            })

    except Exception as exc:
        redis_error = type(exc).__name__

    fallback_used = bool(redis_error) or not results

    if fallback_used:
        rows = (
            await db.execute(
                select(Telemetry).order_by(
                    Telemetry.recorded_at.desc(),
                    Telemetry.id.desc()
                )
            )
        ).scalars().all()

        latest = {}

        for row in rows:
            latest.setdefault(row.vehicle_id, row)

        for vid, row in latest.items():
            if exclude_vehicle_id and vid == exclude_vehicle_id:
                continue

            distance = distance_km(
                latitude,
                longitude,
                row.latitude,
                row.longitude
            )

            if distance <= radius_km:
                results.append({
                    "vehicle_id": vid,
                    "distance_km": round(distance, 3),
                    "longitude": row.longitude,
                    "latitude": row.latitude,
                    "source": "postgres_fallback",
                })

        results.sort(
            key=lambda item: item["distance_km"]
        )

    return {
        "count": len(results),
        "radius_km": radius_km,
        "vehicles": results,
        "fallback_used": fallback_used,
        "redis_error": redis_error,
    }


@router.get("/{vehicle_id}", response_model=VehicleOut)
async def get_vehicle(
    vehicle_id: str,
    db: AsyncSession = Depends(get_db)
):
    vehicle = await db.get(Vehicle, vehicle_id)

    if not vehicle:
        raise HTTPException(
            404,
            detail="Vehicle not found"
        )

    return await _vehicle_with_latest_telemetry(vehicle, db)


@router.get("/{vehicle_id}/history")
async def vehicle_history(
    vehicle_id: str,
    limit: int = Query(100, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
):
    if not await db.get(Vehicle, vehicle_id):
        raise HTTPException(
            404,
            detail="Vehicle not found"
        )

    rows = (
        await db.execute(
            select(Telemetry)
            .where(Telemetry.vehicle_id == vehicle_id)
            .order_by(
                Telemetry.recorded_at.desc(),
                Telemetry.id.desc()
            )
            .limit(limit)
        )
    ).scalars().all()

    return {
        "vehicle_id": vehicle_id,
        "count": len(rows),
        "history": [
            {
                "latitude": row.latitude,
                "longitude": row.longitude,
                "speed": row.speed,
                "heading": row.heading,
                "timestamp": row.recorded_at.isoformat(),
            }
            for row in rows
        ],
    }


@router.get("/{vehicle_id}/cargo")
async def vehicle_cargo(
    vehicle_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Return the active cargo manifest, including delivery destinations
    and delivery status, for a vehicle.
    """

    vehicle = await db.get(Vehicle, vehicle_id)

    if not vehicle:
        raise HTTPException(
            404,
            detail="Vehicle not found"
        )

    packages = (
        await db.execute(
            select(Package).where(
                Package.assigned_vehicle_id == vehicle_id,
                Package.status.in_([
                    "PENDING",
                    "IN_TRANSIT",
                    "REASSIGNED"
                ]),
            )
        )
    ).scalars().all()

    delivery_ids = {
        package.delivery_id
        for package in packages
        if package.delivery_id
    }

    deliveries = []

    if delivery_ids:
        deliveries = (
            await db.execute(
                select(Delivery).where(
                    Delivery.id.in_(delivery_ids)
                )
            )
        ).scalars().all()

    delivery_map = {
        delivery.id: delivery
        for delivery in deliveries
    }

    assigned_weight = sum(
        float(package.weight_kg)
        for package in packages
    )

    return {
        "vehicle_id": vehicle_id,
        "payload_capacity_kg": vehicle.max_payload_kg,
        "assigned_weight_kg": round(assigned_weight, 2),
        "remaining_payload_kg": round(
            vehicle.max_payload_kg - assigned_weight,
            2
        ),
        "packages": [
            {
                "id": package.id,
                "weight_kg": package.weight_kg,
                "dimensions_cm": [
                    package.length_cm,
                    package.width_cm,
                    package.height_cm
                ],
                "delivery_id": package.delivery_id,
                "status": package.status,
                "delivery": (
                    {
                        "id": delivery_map[
                            package.delivery_id
                        ].id,
                        "customer_name": delivery_map[
                            package.delivery_id
                        ].customer_name,
                        "latitude": delivery_map[
                            package.delivery_id
                        ].latitude,
                        "longitude": delivery_map[
                            package.delivery_id
                        ].longitude,
                        "status": delivery_map[
                            package.delivery_id
                        ].status,
                        "priority": delivery_map[
                            package.delivery_id
                        ].priority,
                    }
                    if package.delivery_id in delivery_map
                    else None
                ),
            }
            for package in packages
        ],
    }