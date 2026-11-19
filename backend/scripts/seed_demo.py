import asyncio

from sqlalchemy import select

from app.database import SessionLocal, create_schema
from app.models import Delivery, Geofence, Package, Vehicle

GEOFENCES = [
    ("Indore Warehouse", 22.7196, 75.8577, 450),
    ("Vijay Nagar Hub", 22.7533, 75.8937, 350),
    ("Rau Distribution Point", 22.6395, 75.8070, 400),
    ("Dewas Naka Hub", 22.7905, 75.9140, 350),
    ("Palasia Service Zone", 22.7240, 75.8839, 250),
    ("Super Corridor Hub", 22.7490, 75.8060, 500),
    ("Rajendra Nagar Depot", 22.6760, 75.8390, 350),
    ("Bhawarkua Zone", 22.6930, 75.8660, 300),
    ("MR-10 Logistics Zone", 22.7700, 75.9250, 400),
    ("Pithampur Gate Demo", 22.6100, 75.6900, 500),
]

# id, payload kg, cargo length, width, height
VEHICLES = [
    ("truck_001", 500, 250, 160, 140),
    ("truck_002", 700, 300, 180, 160),
    ("truck_003", 600, 280, 170, 150),
    ("truck_004", 500, 250, 160, 140),
    ("truck_005", 800, 320, 190, 170),
    ("truck_006", 500, 250, 160, 140),
    ("van_007", 300, 200, 130, 120),
    ("van_008", 300, 200, 130, 120),  # recovery helper: keep unassigned
    ("van_009", 350, 220, 140, 125),  # recovery helper: keep unassigned
    ("van_010", 400, 230, 150, 130),
]

# id, customer, latitude, longitude, priority, assigned vehicle
DELIVERIES = [
    ("delivery_001", "Customer A", 22.7300, 75.8700, 5, "truck_001"),
    ("delivery_002", "Customer B", 22.7460, 75.8950, 4, "truck_001"),
    ("delivery_003", "Customer C", 22.7650, 75.9100, 3, "truck_006"),
    ("delivery_004", "Customer D", 22.7050, 75.8450, 4, "truck_006"),
    ("delivery_005", "Customer E", 22.7350, 75.9000, 4, "truck_002"),
    ("delivery_006", "Customer F", 22.7600, 75.8800, 3, "truck_002"),
    ("delivery_007", "Customer G", 22.7000, 75.8700, 5, "truck_003"),
    ("delivery_008", "Customer H", 22.7800, 75.9000, 3, "truck_004"),
    ("delivery_009", "Customer I", 22.6800, 75.8500, 4, "truck_005"),
    ("delivery_010", "Customer J", 22.7200, 75.8400, 3, "van_010"),
]

# id, delivery id, length, width, height, weight kg, assigned vehicle
PACKAGES = [
    ("pkg_001", "delivery_001", 40, 30, 20, 5, "truck_001"),
    ("pkg_002", "delivery_002", 50, 40, 30, 8, "truck_001"),
    ("pkg_003", "delivery_003", 30, 30, 30, 4, "truck_006"),
    ("pkg_004", "delivery_004", 35, 25, 20, 3, "truck_006"),
    ("pkg_005", "delivery_005", 45, 35, 25, 7, "truck_002"),
    ("pkg_006", "delivery_006", 40, 30, 25, 6, "truck_002"),
    ("pkg_007", "delivery_007", 35, 30, 20, 5, "truck_003"),
    ("pkg_008", "delivery_008", 50, 35, 30, 9, "truck_004"),
    ("pkg_009", "delivery_009", 40, 25, 20, 4, "truck_005"),
    ("pkg_010", "delivery_010", 30, 25, 20, 3, "van_010"),
]


async def main():
    await create_schema()

    async with SessionLocal() as db:
        # Geofences: do not duplicate by ID, name, or exact circle.
        for idx, (name, lat, lon, radius) in enumerate(GEOFENCES, 1):
            gid = f"demo_geo_{idx:02d}"
            by_id = await db.get(Geofence, gid)
            by_name = (
                await db.execute(
                    select(Geofence).where(Geofence.name == name)
                )
            ).scalar_one_or_none()
            same_circle = (
                await db.execute(
                    select(Geofence).where(
                        Geofence.latitude == lat,
                        Geofence.longitude == lon,
                        Geofence.radius_m == radius,
                    )
                )
            ).scalar_one_or_none()

            if not by_id and not by_name and not same_circle:
                db.add(
                    Geofence(
                        id=gid, name=name, latitude=lat, longitude=lon,
                        radius_m=radius, active=True, demo_seed=True,
                    )
                )

        # Upsert only the explicitly reserved demo vehicle IDs.
        for vid, payload, length, width, height in VEHICLES:
            vehicle = await db.get(Vehicle, vid)
            if vehicle is None:
                vehicle = Vehicle(id=vid)
                db.add(vehicle)

            vehicle.name = vid.replace("_", " ").title()
            vehicle.vehicle_type = "truck" if vid.startswith("truck") else "van"
            vehicle.capacity_kg = payload
            vehicle.max_payload_kg = payload
            vehicle.cargo_length_cm = length
            vehicle.cargo_width_cm = width
            vehicle.cargo_height_cm = height
            vehicle.active = True
            # Do not reset an existing BREAKDOWN state during a rerun.
            if vehicle.status not in {"BREAKDOWN", "UNAVAILABLE", "INACTIVE"}:
                vehicle.status = "MOVING"

        # Upsert deterministic demo deliveries. Only demo IDs are touched.
        for did, customer, lat, lon, priority, vehicle_id in DELIVERIES:
            delivery = await db.get(Delivery, did)
            if delivery is None:
                delivery = Delivery(id=did)
                db.add(delivery)

            delivery.customer_name = customer
            delivery.latitude = lat
            delivery.longitude = lon
            delivery.priority = priority
            delivery.service_minutes = 5
            delivery.assigned_vehicle_id = vehicle_id
            # Re-seeding restores demo workload to pending state.
            delivery.status = "PENDING"

        await db.flush()

        # Upsert package records after delivery rows exist.
        for pid, did, length, width, height, weight, vehicle_id in PACKAGES:
            package = await db.get(Package, pid)
            if package is None:
                package = Package(id=pid)
                db.add(package)

            package.description = f"Demo parcel {pid}"
            package.delivery_id = did
            package.assigned_vehicle_id = vehicle_id
            package.length_cm = length
            package.width_cm = width
            package.height_cm = height
            package.weight_kg = weight
            package.status = "PENDING"
            package.allowed_rotations = "ALL"

        await db.commit()

    print(
        "Demo seed complete: 10 geofences, 10 vehicles, "
        "10 deliveries, 10 packages. Existing non-demo records preserved."
    )


if __name__ == "__main__":
    asyncio.run(main())
