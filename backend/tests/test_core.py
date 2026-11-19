from app.services.geo import distance_km
from app.services.packing import pack_packages
from app.services.routing import optimize_route
from app.services.delay_detector import expected_speed_kmh
from datetime import datetime
from zoneinfo import ZoneInfo


def test_distance_zero_and_indore_scale():
    assert distance_km(22.7, 75.8, 22.7, 75.8) == 0
    assert 1.0 < distance_km(22.7600, 75.9200, 22.7600, 75.9400) < 3.0


def test_3d_packing_feasible_and_non_overlapping():
    vehicle = {"cargo_length_cm": 100, "cargo_width_cm": 100, "cargo_height_cm": 100, "max_payload_kg": 30}
    packages = [{"id": "a", "delivery_id": "d1", "length_cm": 40, "width_cm": 30, "height_cm": 20, "weight_kg": 5},
                {"id": "b", "delivery_id": "d2", "length_cm": 50, "width_cm": 40, "height_cm": 30, "weight_kg": 8}]
    result = pack_packages(vehicle, packages, {"d1": 0, "d2": 1})
    assert result["feasible"] is True
    assert len(result["placements"]) == 2
    assert result["unpacked"] == []


def test_3d_packing_rejects_payload_overflow():
    result = pack_packages({"cargo_length_cm": 100, "cargo_width_cm": 100, "cargo_height_cm": 100, "max_payload_kg": 5},
        [{"id": "heavy", "length_cm": 10, "width_cm": 10, "height_cm": 10, "weight_kg": 6}])
    assert not result["feasible"]
    assert "payload capacity" in result["failure_reason"]


def test_route_optimizer_returns_each_stop_once():
    result = optimize_route((22.7, 75.8), [{"id": "a", "latitude": 22.71, "longitude": 75.81},
        {"id": "b", "latitude": 22.72, "longitude": 75.82}, {"id": "c", "latitude": 22.705, "longitude": 75.805}])
    assert {x["id"] for x in result["stops"]} == {"a", "b", "c"}
    assert [x["sequence"] for x in result["stops"]] == [1, 2, 3]
    assert result["total_distance_km"] > 0


def test_expected_speed_windows_are_timezone_aware():
    tz = ZoneInfo("Asia/Kolkata")
    assert expected_speed_kmh(datetime(2026, 10, 29, 9, 0, tzinfo=tz)) == 25
    assert expected_speed_kmh(datetime(2026, 10, 29, 18, 0, tzinfo=tz)) == 20
    assert expected_speed_kmh(datetime(2026, 10, 29, 13, 0, tzinfo=tz)) == 40

class FakeRedis:
    def __init__(self):
        self.data = {}
    async def hgetall(self, key):
        return dict(self.data.get(key, {}))
    async def hset(self, key, mapping):
        self.data.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})
    async def expire(self, key, seconds):
        return True
    async def delete(self, key):
        self.data.pop(key, None)


def test_delay_alert_once_then_recover_and_retrigger():
    import asyncio
    from app.services.delay_detector import evaluate_delay
    async def scenario():
        redis = FakeRedis()
        assert await evaluate_delay(redis, "v1", 5, 40) is None
        assert await evaluate_delay(redis, "v1", 5, 40) is None
        started = await evaluate_delay(redis, "v1", 5, 40)
        assert started["transition"] == "started"
        assert await evaluate_delay(redis, "v1", 5, 40) is None
        recovered = await evaluate_delay(redis, "v1", 30, 40)
        assert recovered["transition"] == "recovered"
        assert await evaluate_delay(redis, "v1", 5, 40) is None
        assert await evaluate_delay(redis, "v1", 5, 40) is None
        started_again = await evaluate_delay(redis, "v1", 5, 40)
        assert started_again["transition"] == "started"
    asyncio.run(scenario())


def test_route_schedule_reports_time_window_lateness():
    from datetime import datetime, timezone, timedelta
    departure = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    result = optimize_route((22.7, 75.8), [{"id": "late-stop", "latitude": 22.8, "longitude": 75.9,
        "time_window_end": departure - timedelta(minutes=1), "priority": 5, "service_minutes": 10}], departure_time=departure)
    assert result["time_windows_considered"] is True
    assert result["total_late_minutes"] > 0
    assert result["stops"][0]["estimated_arrival"].endswith("+00:00")


def test_identical_speeds_with_location_change_are_congestion_not_breakdown():
    import asyncio
    from datetime import datetime, timezone, timedelta
    from app.services.delay_detector import evaluate_delay

    async def scenario():
        redis = FakeRedis()
        start = datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc)
        result = None
        for i in range(3):
            result = await evaluate_delay(
                redis, "van_traffic", 5, 40,
                22.7600 + (i * 0.001), 75.9200, start + timedelta(seconds=i * 10)
            )
        assert result["transition"] == "started"
        assert result["movement_classification"] == "TRAFFIC_CONGESTION"
        assert result["breakdown_confirmed"] is False

    asyncio.run(scenario())


def test_prolonged_stationary_breakdown_requires_elapsed_time_and_configured_vehicle():
    import asyncio
    from datetime import datetime, timezone, timedelta
    from app.config import settings
    from app.services.delay_detector import evaluate_delay

    async def scenario():
        assert settings.breakdown_vehicle_ids, "Configure at least one breakdown demo vehicle"
        vehicle_id = sorted(settings.breakdown_vehicle_ids)[0]
        threshold = settings.prolonged_stop_seconds
        start = datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc)
        redis = FakeRedis()

        await evaluate_delay(
            redis, vehicle_id, 0, 40, 22.7000, 75.8400, start
        )
        before = await evaluate_delay(
            redis, vehicle_id, 0, 40, 22.7000, 75.8400,
            start + timedelta(seconds=threshold - 1)
        )
        assert before is None
        at_threshold = await evaluate_delay(
            redis, vehicle_id, 0, 40, 22.7000, 75.8400,
            start + timedelta(seconds=threshold)
        )
        assert at_threshold is not None
        assert at_threshold["movement_classification"] == "BREAKDOWN"
        state = await redis.hgetall(f"movement:state:{vehicle_id}")
        assert state["breakdown_confirmed"] == "1"

        # Movement telemetry does not clear a confirmed breakdown.
        await evaluate_delay(
            redis, vehicle_id, 35, 40, 22.7050, 75.8450,
            start + timedelta(seconds=threshold + 5)
        )
        state = await redis.hgetall(f"movement:state:{vehicle_id}")
        assert state["movement_classification"] == "BREAKDOWN"
        assert state["breakdown_confirmed"] == "1"

        redis2 = FakeRedis()
        for seconds in (0, threshold):
            await evaluate_delay(
                redis2, "van_not_configured", 0, 40, 22.7000, 75.8400,
                start + timedelta(seconds=seconds)
            )
        state2 = await redis2.hgetall("movement:state:van_not_configured")
        assert state2["movement_classification"] == "PROLONGED_STOPPAGE"
        assert state2["breakdown_confirmed"] == "0"

    asyncio.run(scenario())
