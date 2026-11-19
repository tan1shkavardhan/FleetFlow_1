
import argparse
import asyncio
import hashlib
import math
from datetime import datetime, timezone
from uuid import uuid4

import httpx


# ---------------------------------------------------------
# FLEET CONFIGURATION
# ---------------------------------------------------------

CITY_CENTER = (22.7196, 75.8577)

TRUCK_001_WAYPOINTS = [
    (22.7100, 75.8500),
    (22.7140, 75.8520),
    (22.7196, 75.8577),
    (22.7240, 75.8630),
    (22.7300, 75.8700),
    (22.7360, 75.8770),
    (22.7420, 75.8840),
    (22.7480, 75.8900),
]

# Staggered incidents, measured from simulator launch.
# These are demo labels; the backend detects stationary movement,
# not the actual mechanical cause.
BREAKDOWN_SCENARIOS = {
    "truck_002": {
        "start_after_seconds": 120,
        "reason": "Prolonged stationary breakdown",
    },
    "truck_015": {
        "start_after_seconds": 300,
        "reason": "Simulated engine failure",
    },
    "van_012": {
        "start_after_seconds": 420,
        "reason": "Simulated tyre failure",
    },
    "truck_025": {
        "start_after_seconds": 600,
        "reason": "Simulated engine overheating",
    },
}

ACTIVE_RECOVERY_STATUSES = {
    "ASSIGNED",
    "EN_ROUTE_TO_INCIDENT",
    "ARRIVED_AT_INCIDENT",
    "HANDOVER_IN_PROGRESS",
    "HANDOVER_COMPLETED",
    "IN_TRANSIT_TO_DESTINATION",
}


# ---------------------------------------------------------
# RESPONSE HELPERS
# ---------------------------------------------------------

def extract_records(payload, preferred_keys):
    """Handle direct arrays and common API response wrappers."""

    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        for key in preferred_keys:
            value = payload.get(key)

            if isinstance(value, list):
                return value

            if isinstance(value, dict):
                result = extract_records(
                    value,
                    (
                        "items",
                        "results",
                        "vehicles",
                        "packages",
                        "deliveries",
                        "data",
                    ),
                )

                if result is not None:
                    return result

    return None


async def fetch_vehicles(client):
    """Fetch all active vehicles registered in FleetFlow."""

    response = await client.get("/vehicles/")
    response.raise_for_status()

    vehicles = extract_records(
        response.json(),
        ("data", "vehicles", "items", "results"),
    )

    if vehicles is None:
        raise RuntimeError(
            "Could not identify the vehicle list in GET /vehicles/ response."
        )

    active_vehicles = [
        vehicle
        for vehicle in vehicles
        if vehicle.get("active", True)
    ]

    if not active_vehicles:
        raise RuntimeError("No active vehicles were returned by the API.")

    return active_vehicles


async def fetch_recovery_targets(client):
    """Read active recovery plans and determine helper destinations."""

    try:
        response = await client.get("/recovery/plans")

        if not response.is_success:
            return {}

        plans = extract_records(
            response.json(),
            ("data", "plans", "items", "results"),
        )

        if plans is None:
            return {}

    except (httpx.HTTPError, ValueError):
        return {}

    targets = {}

    for recovery in plans:
        if recovery.get("status") not in ACTIVE_RECOVERY_STATUSES:
            continue

        helper_id = recovery.get("helper_vehicle_id")

        if not helper_id:
            continue

        plan_data = recovery.get("plan") or {}

        if recovery.get("status") in {
            "ASSIGNED",
            "EN_ROUTE_TO_INCIDENT",
            "ARRIVED_AT_INCIDENT",
            "HANDOVER_IN_PROGRESS",
        }:
            origin = plan_data.get("origin") or {}

            latitude = origin.get("latitude")
            longitude = origin.get("longitude")

            if latitude is not None and longitude is not None:
                targets[helper_id] = (
                    float(latitude),
                    float(longitude),
                )

        else:
            destinations = plan_data.get("destinations") or []

            destination_index = int(
                plan_data.get("destination_index", 0)
            )

            if 0 <= destination_index < len(destinations):
                destination = destinations[destination_index]

                latitude = destination.get("latitude")
                longitude = destination.get("longitude")

                if latitude is not None and longitude is not None:
                    targets[helper_id] = (
                        float(latitude),
                        float(longitude),
                    )

    return targets


# ---------------------------------------------------------
# POSITION GENERATION
# ---------------------------------------------------------

def stable_vehicle_position(vehicle_id, index):
    """Generate a deterministic starting position around Indore."""

    digest = hashlib.sha256(
        vehicle_id.encode("utf-8")
    ).digest()

    phase_offset = int.from_bytes(
        digest[:4],
        "big",
    ) / 2**32

    angle = index * 2.399963229728653 + phase_offset
    radius = 0.004 + (index % 12) * 0.0015

    latitude = (
        CITY_CENTER[0] + radius * math.sin(angle)
    )

    longitude = (
        CITY_CENTER[1] + radius * math.cos(angle)
    )

    return latitude, longitude


def interpolate_route(waypoints, tick, steps_per_leg=15):
    """Interpolate smoothly along a one-way route."""

    if len(waypoints) < 2:
        return waypoints[0]

    leg = tick // steps_per_leg

    if leg >= len(waypoints) - 1:
        return waypoints[-1]

    fraction = (tick % steps_per_leg) / steps_per_leg

    start = waypoints[leg]
    end = waypoints[leg + 1]

    return (
        start[0] + (end[0] - start[0]) * fraction,
        start[1] + (end[1] - start[1]) * fraction,
    )


def move_towards(current, target, step_degrees=0.00018):
    """Move a vehicle toward a recovery destination."""

    latitude, longitude = current
    target_lat, target_lon = float(target[0]), float(target[1])

    delta_lat = target_lat - latitude
    delta_lon = target_lon - longitude

    distance = math.hypot(delta_lat, delta_lon)

    if distance <= step_degrees:
        return (target_lat, target_lon), 12.0

    scale = step_degrees / distance

    return (
        (
            latitude + delta_lat * scale,
            longitude + delta_lon * scale,
        ),
        28.0,
    )


# ---------------------------------------------------------
# TELEMETRY
# ---------------------------------------------------------

def create_telemetry_payload(
    vehicle_id,
    position,
    speed,
    session_id,
    tick,
):
    latitude, longitude = position

    return {
        "event_id": f"{session_id}:{vehicle_id}:{tick}",
        "vehicle_id": vehicle_id,
        "latitude": round(latitude, 7),
        "longitude": round(longitude, 7),
        "speed": round(float(speed), 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def send_telemetry(
    client,
    semaphore,
    vehicle_id,
    payload,
):
    async with semaphore:
        try:
            response = await client.post(
                "/telemetry/",
                json=payload,
            )

            if response.is_error:
                print(
                    f"ERROR {vehicle_id}: "
                    f"{response.status_code} "
                    f"{response.text[:160]}"
                )
                return False

            return True

        except httpx.HTTPError as exc:
            print(f"Telemetry error {vehicle_id}: {exc}")
            return False


# ---------------------------------------------------------
# MAIN SIMULATION
# ---------------------------------------------------------

async def run(
    base_url,
    count,
    interval,
    delay_vehicle,
    enable_breakdowns=True,
):
    timeout = httpx.Timeout(
        10.0,
        connect=5.0,
    )

    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=timeout,
    ) as client:

        try:
            vehicles = await fetch_vehicles(client)

        except (
            httpx.HTTPError,
            RuntimeError,
            ValueError,
        ) as exc:
            raise SystemExit(
                f"Vehicle discovery failed: {exc}"
            )

        if count is not None:
            vehicles = vehicles[:count]

        vehicle_ids = [
            vehicle["id"]
            for vehicle in vehicles
            if vehicle.get("id")
        ]

        if not vehicle_ids:
            raise SystemExit(
                "No usable vehicle IDs were discovered."
            )

        available_ids = set(vehicle_ids)

        if delay_vehicle and delay_vehicle not in available_ids:
            raise SystemExit(
                f"Delay vehicle '{delay_vehicle}' "
                "is not in the selected active fleet."
            )

        scenarios = (
            BREAKDOWN_SCENARIOS
            if enable_breakdowns
            else {}
        )

        missing_scenarios = set(scenarios) - available_ids

        if missing_scenarios:
            raise SystemExit(
                "Selected fleet is missing breakdown vehicles: "
                f"{sorted(missing_scenarios)}. "
                "Run the full fleet simulation."
            )

        # Use database coordinates when available.
        # Generate deterministic positions otherwise.
        base_positions = {}

        for index, vehicle in enumerate(vehicles):
            vehicle_id = vehicle["id"]

            latitude = vehicle.get("latitude")
            longitude = vehicle.get("longitude")

            if latitude is not None and longitude is not None:
                base_positions[vehicle_id] = (
                    float(latitude),
                    float(longitude),
                )
            else:
                base_positions[vehicle_id] = stable_vehicle_position(
                    vehicle_id,
                    index,
                )

        positions = dict(base_positions)

        # Freeze the position at which each breakdown begins.
        breakdown_positions = {}

        session_id = uuid4().hex
        tick = 0

        semaphore = asyncio.Semaphore(25)

        loop = asyncio.get_running_loop()
        simulation_started_at = loop.time()

        announced_scenarios = set()

        print("\n========== FLEETFLOW SIMULATION ==========")
        print(f"Active vehicles discovered: {len(vehicle_ids)}")
        print(f"Vehicles being simulated: {len(vehicle_ids)}")
        print(f"Telemetry interval: {interval} seconds")
        print(f"Breakdown scenarios enabled: {enable_breakdowns}")
        print(f"Delay vehicle: {delay_vehicle or 'disabled'}")
        print("Recovery helpers: van_008 and van_009, if available")
        print("truck_001: one-way route through Indore")

        if enable_breakdowns:
            print("\nScheduled breakdown scenarios:")

            for vehicle_id, scenario in scenarios.items():
                print(
                    f"  {vehicle_id}: {scenario['reason']} "
                    f"at +{scenario['start_after_seconds']} seconds"
                )

        print("\nBackend stationary confirmation threshold: 120 seconds")
        print("Existing vehicle specifications will not be overwritten.")
        print("==========================================\n")

        if len(vehicle_ids) != 100:
            print(
                f"WARNING: Expected 100 vehicles, "
                f"but discovered {len(vehicle_ids)} active vehicles."
            )

        try:
            while True:
                elapsed = loop.time() - simulation_started_at

                helper_targets = await fetch_recovery_targets(client)

                telemetry_tasks = []

                for index, vehicle_id in enumerate(vehicle_ids):
                    base_lat, base_lon = base_positions[vehicle_id]

                    # Recovery helper movement takes priority.
                    if vehicle_id in helper_targets:
                        (latitude, longitude), speed = move_towards(
                            positions[vehicle_id],
                            helper_targets[vehicle_id],
                        )

                    # Scheduled breakdown scenarios.
                    elif vehicle_id in scenarios:
                        scenario = scenarios[vehicle_id]

                        incident_started = (
                            elapsed >= scenario["start_after_seconds"]
                        )

                        if incident_started:
                            if vehicle_id not in breakdown_positions:
                                breakdown_positions[vehicle_id] = (
                                    positions[vehicle_id]
                                )

                                print(
                                    "\n[INCIDENT STARTED] "
                                    f"{vehicle_id} | "
                                    f"{scenario['reason']} | "
                                    "stationary telemetry started"
                                )

                                announced_scenarios.add(vehicle_id)

                            latitude, longitude = (
                                breakdown_positions[vehicle_id]
                            )

                            speed = 0.0

                        else:
                            # Before its incident, move normally.
                            phase = tick * 0.02 + index * 0.7

                            latitude = (
                                base_lat + 0.0018 * math.sin(phase)
                            )

                            longitude = (
                                base_lon + 0.0018 * math.cos(phase)
                            )

                            speed = 22.0 + (index % 4) * 5.0

                    # Dedicated one-way truck route.
                    elif vehicle_id == "truck_001":
                        latitude, longitude = interpolate_route(
                            TRUCK_001_WAYPOINTS,
                            tick,
                        )

                        route_duration = (
                            (len(TRUCK_001_WAYPOINTS) - 1) * 15
                        )

                        speed = (
                            0.0
                            if tick >= route_duration
                            else 25.0
                        )

                    else:
                        # Each vehicle follows a different deterministic path.
                        phase = tick * 0.02 + index * 0.7

                        latitude = (
                            base_lat + 0.0018 * math.sin(phase)
                        )

                        longitude = (
                            base_lon + 0.0018 * math.cos(phase)
                        )

                        if delay_vehicle and vehicle_id == delay_vehicle:
                            speed = 5.0 if tick < 20 else 32.0

                        elif vehicle_id in ("van_008", "van_009"):
                            speed = 28.0

                        else:
                            speed = 22.0 + (index % 4) * 5.0

                    position = (latitude, longitude)
                    positions[vehicle_id] = position

                    payload = create_telemetry_payload(
                        vehicle_id=vehicle_id,
                        position=position,
                        speed=speed,
                        session_id=session_id,
                        tick=tick,
                    )

                    telemetry_tasks.append(
                        send_telemetry(
                            client,
                            semaphore,
                            vehicle_id,
                            payload,
                        )
                    )

                results = await asyncio.gather(*telemetry_tasks)

                accepted = sum(results)
                failed = len(results) - accepted

                if tick % 5 == 0:
                    print(
                        f"Tick {tick:05d} | "
                        f"Accepted: {accepted}/{len(vehicle_ids)} | "
                        f"Failed: {failed} | "
                        f"Recovery helpers moving: {len(helper_targets)} | "
                        f"Breakdowns started: "
                        f"{len(announced_scenarios)}/{len(scenarios)}"
                    )

                tick += 1
                await asyncio.sleep(interval)

        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nFleetFlow simulation stopped.")


# ---------------------------------------------------------
# COMMAND LINE
# ---------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="FleetFlow 100-vehicle recovery demo simulator"
    )

    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000",
    )

    parser.add_argument(
        "--vehicles",
        type=int,
        default=None,
        help="Maximum active vehicles to simulate. Default: all.",
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Telemetry interval in seconds. Minimum: 0.25.",
    )

    parser.add_argument(
        "--delay-vehicle",
        default="truck_003",
        help="Vehicle ID for delay simulation, or 'none'.",
    )

    parser.add_argument(
        "--no-breakdowns",
        action="store_true",
        help="Disable the four scheduled breakdown scenarios.",
    )

    args = parser.parse_args()

    if args.vehicles is not None and args.vehicles < 1:
        parser.error("--vehicles must be at least 1")

    if args.interval < 0.25:
        parser.error("--interval must be at least 0.25 seconds")

    delay_vehicle = (
        None
        if args.delay_vehicle.lower() == "none"
        else args.delay_vehicle
    )

    asyncio.run(
        run(
            base_url=args.url,
            count=args.vehicles,
            interval=args.interval,
            delay_vehicle=delay_vehicle,
            enable_breakdowns=not args.no_breakdowns,
        )
    )
