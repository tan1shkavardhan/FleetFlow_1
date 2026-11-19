"""Reset only FleetFlow's known demo vehicles and detector state.

Does not delete telemetry history, deliveries, packages, recovery plans, or alerts.
Run from the backend directory: python scripts/reset_demo.py
"""
import asyncio

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Vehicle
from app.config import settings
from app.redis_client import redis_client

DEMO_VEHICLE_IDS = [
    "truck_001", "truck_002", "truck_003", "truck_004", "truck_005",
    "truck_006", "van_007", "van_008", "van_009", "van_010",
]


async def reset():
    async with SessionLocal() as db:
        result = await db.execute(
            select(Vehicle).where(Vehicle.id.in_(DEMO_VEHICLE_IDS))
        )
        vehicles = result.scalars().all()
        found = {vehicle.id for vehicle in vehicles}

        for vehicle in vehicles:
            if vehicle.active:
                vehicle.status = "MOVING"

        await db.commit()

    # Clear only per-demo-vehicle runtime state. Keep telemetry stream/history intact.
    deleted_keys = 0
    for vehicle_id in DEMO_VEHICLE_IDS:
        keys = [
            f"movement:state:{vehicle_id}",
            f"breakdown:alerted:{vehicle_id}",
            f"vehicle:latest:{vehicle_id}",
        ]
        keys.extend([
            key async for key in redis_client.scan_iter(
                match=f"geofence:state:{vehicle_id}:*"
            )
        ])
        if keys:
            deleted_keys += await redis_client.delete(*keys)
        await redis_client.zrem(settings.vehicle_geo_key, vehicle_id)

    print("FleetFlow demo reset complete.")
    print(f"Vehicles reset: {len(found)}/{len(DEMO_VEHICLE_IDS)}")
    print(f"Redis runtime keys deleted: {deleted_keys}")
    missing = sorted(set(DEMO_VEHICLE_IDS) - found)
    if missing:
        print("Not found (ignored): " + ", ".join(missing))
    print("Telemetry history, Redis telemetry stream, packages, deliveries, alerts, and recovery plans were preserved.")
    print("Next: python simulator/driver_simulator.py --vehicles 10 --interval 2")


if __name__ == "__main__":
    asyncio.run(reset())
