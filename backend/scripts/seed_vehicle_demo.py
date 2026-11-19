import asyncio
import argparse
import random
import hashlib

from sqlalchemy import select, delete

from app.database import SessionLocal
from app.models import Vehicle, Delivery, Package
from app.services.packing import pack_packages


SEED = 20261119
TARGET_VEHICLES = 100

DEL_PREFIX = "demo100_del_"
PKG_PREFIX = "demo100_pkg_"

TRUCK_TARGET = 70
VAN_TARGET = 30

TRUCK = {
    "vehicle_type": "truck",
    "capacity_kg": 900.0,
    "max_payload_kg": 900.0,
    "cargo_length_cm": 360.0,
    "cargo_width_cm": 200.0,
    "cargo_height_cm": 190.0,
}

VAN = {
    "vehicle_type": "van",
    "capacity_kg": 400.0,
    "max_payload_kg": 400.0,
    "cargo_length_cm": 240.0,
    "cargo_width_cm": 155.0,
    "cargo_height_cm": 145.0,
}


def key(value):
    return hashlib.sha1(value.encode()).hexdigest()[:10]


async def create_fleet(db, existing):
    """Preserve existing vehicles and add enough to reach 100."""

    ids = {v.id for v in existing}

    trucks = sum(v.vehicle_type == "truck" for v in existing)
    vans = sum(v.vehicle_type == "van" for v in existing)

    added = []
    truck_number = 1
    van_number = 1

    while len(existing) + len(added) < TARGET_VEHICLES:

        if trucks < TRUCK_TARGET:
            kind = "truck"
        else:
            kind = "van"

        if kind == "truck":
            while f"truck_{truck_number:03d}" in ids:
                truck_number += 1

            vehicle_id = f"truck_{truck_number:03d}"
            truck_number += 1
            specs = TRUCK
            trucks += 1

        else:
            while f"van_{van_number:03d}" in ids:
                van_number += 1

            vehicle_id = f"van_{van_number:03d}"
            van_number += 1
            specs = VAN
            vans += 1

        vehicle = Vehicle(
            id=vehicle_id,
            name=vehicle_id.replace("_", " ").title(),
            vehicle_type=specs["vehicle_type"],
            capacity_kg=specs["capacity_kg"],
            max_payload_kg=specs["max_payload_kg"],
            cargo_length_cm=specs["cargo_length_cm"],
            cargo_width_cm=specs["cargo_width_cm"],
            cargo_height_cm=specs["cargo_height_cm"],
            active=True,
            status="MOVING",
        )

        db.add(vehicle)
        added.append(vehicle)
        ids.add(vehicle_id)

    await db.flush()
    return existing + added


def generate_dimensions(rng, vehicle):
    """Generate parcel dimensions relative to its assigned cargo bay."""

    def dimension(bay):
        low = max(8, int(bay * 0.08))
        high = min(65, int(bay * 0.24))
        return float(rng.randint(low, max(low, high)))

    return (
        dimension(vehicle.cargo_length_cm),
        dimension(vehicle.cargo_width_cm),
        dimension(vehicle.cargo_height_cm),
    )


def package_dict(p):
    return {
        "id": p.id,
        "delivery_id": p.delivery_id,
        "length_cm": p.length_cm,
        "width_cm": p.width_cm,
        "height_cm": p.height_cm,
        "weight_kg": p.weight_kg,
        "allowed_rotations": p.allowed_rotations,
    }


def delivery_order(deliveries):
    ordered = sorted(
        deliveries,
        key=lambda d: (-d.priority, d.id)
    )
    return {d.id: i for i, d in enumerate(ordered)}


async def generate_vehicle_cargo(db, vehicle, rng):
    """Generate cargo and validate combined existing + new load."""

    existing_packages = (
        await db.execute(
            select(Package).where(
                Package.assigned_vehicle_id == vehicle.id,
                Package.status.in_(
                    ["PENDING", "IN_TRANSIT", "REASSIGNED"]
                )
            )
        )
    ).scalars().all()

    existing_delivery_ids = {
        p.delivery_id for p in existing_packages if p.delivery_id
    }

    existing_deliveries = []

    if existing_delivery_ids:
        existing_deliveries = (
            await db.execute(
                select(Delivery).where(
                    Delivery.id.in_(existing_delivery_ids)
                )
            )
        ).scalars().all()

    current_weight = sum(
        float(p.weight_kg) for p in existing_packages
    )

    remaining = vehicle.max_payload_kg - current_weight

    if remaining <= 0:
        raise ValueError(
            f"{vehicle.id}: no remaining payload capacity"
        )

    count = rng.randint(2, 4)
    vehicle_key = key(vehicle.id)

    for attempt in range(30):

        count = max(2, count - (1 if attempt and attempt % 5 == 0 else 0))
        weight_limit = min(18.0, remaining / count * 0.65)

        deliveries = []
        packages = []
        new_package_dicts = []

        for i in range(1, count + 1):

            delivery_id = f"{DEL_PREFIX}{vehicle_key}_{i:02d}"
            package_id = f"{PKG_PREFIX}{vehicle_key}_{i:02d}"

            # Generate realistic delivery destinations around Indore.
            lat = 22.7196 + rng.uniform(-0.045, 0.045)
            lon = 75.8577 + rng.uniform(-0.045, 0.045)

            delivery = Delivery(
                id=delivery_id,
                customer_name=f"Demo {vehicle.name} Customer {i}",
                latitude=round(lat, 7),
                longitude=round(lon, 7),
                priority=rng.randint(1, 5),
                service_minutes=rng.choice([5, 7, 10, 12]),
                assigned_vehicle_id=vehicle.id,
                status="PENDING",
            )

            length, width, height = generate_dimensions(
                rng, vehicle
            )

            weight = round(
                rng.uniform(0.5, weight_limit), 2
            )

            rotation = rng.choice(
                ["ALL", "ALL", "UPRIGHT"]
            )

            package = Package(
                id=package_id,
                description=f"Demo parcel for {vehicle.name}",
                length_cm=length,
                width_cm=width,
                height_cm=height,
                weight_kg=weight,
                delivery_id=delivery_id,
                assigned_vehicle_id=vehicle.id,
                allowed_rotations=rotation,
                status="PENDING",
            )

            deliveries.append(delivery)
            packages.append(package)
            new_package_dicts.append(package_dict(package))

        all_deliveries = existing_deliveries + deliveries

        all_packages = [
            package_dict(p) for p in existing_packages
        ] + new_package_dicts

        result = pack_packages(
            {
                "id": vehicle.id,
                "cargo_length_cm": vehicle.cargo_length_cm,
                "cargo_width_cm": vehicle.cargo_width_cm,
                "cargo_height_cm": vehicle.cargo_height_cm,
                "capacity_kg": vehicle.capacity_kg,
                "max_payload_kg": vehicle.max_payload_kg,
            },
            all_packages,
            delivery_order(all_deliveries),
            apply_accessibility=True,
        )

        if result.get("feasible"):
            return deliveries, packages, result

    raise ValueError(
        f"3D packing failed for {vehicle.id}. "
        "No generated data will be committed."
    )


async def seed(seed, dry_run):

    rng = random.Random(seed)

    async with SessionLocal() as db:

        transaction = await db.begin()

        try:

            vehicles = (
                await db.execute(
                    select(Vehicle).order_by(Vehicle.id)
                )
            ).scalars().all()

            if not vehicles:
                raise RuntimeError(
                    "Seed/register the initial vehicles first."
                )

            if len(vehicles) > TARGET_VEHICLES:
                raise RuntimeError(
                    f"Already have {len(vehicles)} vehicles. "
                    "Increase TARGET_VEHICLES explicitly."
                )

            vehicles = await create_fleet(db, vehicles)

            # Delete only this generator's previous output.
            await db.execute(
                delete(Package).where(
                    Package.id.like(f"{PKG_PREFIX}%")
                )
            )

            await db.execute(
                delete(Delivery).where(
                    Delivery.id.like(f"{DEL_PREFIX}%")
                )
            )

            total_packages = 0

            for vehicle in vehicles:

                deliveries, packages, result = (
                    await generate_vehicle_cargo(
                        db, vehicle, rng
                    )
                )

                if not dry_run:
                    db.add_all(deliveries)
                    await db.flush()
                    db.add_all(packages)

                total_packages += len(packages)

                print(
                    f"{vehicle.id}: "
                    f"{len(packages)} packages | "
                    f"{result['total_weight_kg']:.1f}/"
                    f"{vehicle.max_payload_kg:.1f} kg | "
                    f"3D placements: "
                    f"{len(result['placements'])}"
                )

            if dry_run:
                await transaction.rollback()
            else:
                await transaction.commit()

        except Exception:
            if transaction.is_active:
                await transaction.rollback()
            raise

    print("\nFleetFlow demo generation finished.")
    print("Vehicles:", len(vehicles))
    print("Generated packages:", total_packages)
    print("Generated deliveries:", total_packages)
    print("Seed:", seed)
    print("Dry run: no changes saved." if dry_run else "Database committed.")


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    asyncio.run(seed(args.seed, args.dry_run))
