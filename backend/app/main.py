import logging
from contextlib import asynccontextmanager

import sqlalchemy
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import create_schema, engine
from app.redis_client import redis_client
from app.routers import (
    alerts,
    cargo,
    geofences,
    optimization,
    telemetry,
    vehicles,
    websocket,
)
from app.services.telemetry_worker import telemetry_worker
from app.services.websocket_manager import manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):

    # Initialize database schema.
    await create_schema()

    # Check Redis and start the background worker.
    try:
        await redis_client.ping()
        await telemetry_worker.start()
        logger.info("Redis connected and telemetry worker started")

    except Exception:
        logger.exception(
            "Redis unavailable during startup. " "Telemetry worker will not start."
        )

    yield

    # Gracefully stop background processing.
    await telemetry_worker.stop()

    # Close infrastructure connections.
    await redis_client.aclose()
    await engine.dispose()

    logger.info("FleetFlow shutdown complete")


app = FastAPI(
    title=settings.app_name,
    version="2.0.0",
    description=(
        "Real-time fleet monitoring, 3D cargo packing, "
        "helper allocation and route recovery."
    ),
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# API routers.
app.include_router(telemetry.router)
app.include_router(vehicles.router)
app.include_router(geofences.router)
app.include_router(cargo.router)
app.include_router(optimization.router)
app.include_router(alerts.router)
app.include_router(websocket.router)


@app.get("/")
async def root():
    return {
        "name": settings.app_name,
        "version": "2.0.0",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health")
async def health():

    database_ok = False
    redis_ok = False

    try:
        async with engine.connect() as connection:
            await connection.execute(sqlalchemy.text("SELECT 1"))

        database_ok = True

    except Exception:
        logger.exception("Database health check failed")

    try:
        await redis_client.ping()
        redis_ok = True

    except Exception:
        logger.exception("Redis health check failed")

    return {
        "status": ("healthy" if database_ok and redis_ok else "degraded"),
        "database": database_ok,
        "redis": redis_ok,
        "websocket_connections": manager.connection_count,
    }
