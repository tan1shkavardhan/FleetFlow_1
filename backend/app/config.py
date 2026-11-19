from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "FleetFlow API"
    environment: str = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    database_url: str = (
        "postgresql+asyncpg://fleetflow:fleetflow@localhost:1543/fleetflow"
    )
    redis_url: str = "redis://localhost:6379/0"

    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173"
    )

    vehicle_geo_key: str = "vehicles:locations"
    telemetry_stream_key: str = "telemetry:stream"
    telemetry_stream_maxlen: int = 100000

    delay_threshold_ratio: float = 0.4
    delay_consecutive_limit: int = 3
    delay_recovery_ratio: float = 0.6

    # A configured breakdown vehicle must remain stationary
    # for this many seconds before BREAKDOWN is confirmed.
    prolonged_stop_seconds: int = 120

    default_nearby_radius_km: float = 20.0

    # All four demo incident vehicles are eligible for breakdown detection.
    # An environment variable named DEMO_BREAKDOWN_VEHICLES can override this.
    demo_breakdown_vehicles: str = (
        "truck_002,truck_015,van_012,truck_025"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [
            value.strip()
            for value in self.cors_origins.split(",")
            if value.strip()
        ]

    @property
    def breakdown_vehicle_ids(self) -> set[str]:
        return {
            value.strip()
            for value in self.demo_breakdown_vehicles.split(",")
            if value.strip()
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()