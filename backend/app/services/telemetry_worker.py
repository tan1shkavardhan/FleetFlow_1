import asyncio
import json
import logging

from app.config import settings
from app.redis_client import redis_client
from app.services.websocket_manager import manager


logger = logging.getLogger(__name__)

CONSUMER_GROUP = "fleetflow-websocket-group"
CONSUMER_NAME = "fleetflow-api"
BATCH_SIZE = 100


class TelemetryWorker:

    def __init__(self):
        self.running = False
        self.task = None

    async def start(self):
        if self.running:
            return

        self.running = True
        self.task = asyncio.create_task(self._consume())

        logger.info("Telemetry worker started")

    async def stop(self):
        self.running = False

        if self.task:
            self.task.cancel()

            try:
                await self.task
            except asyncio.CancelledError:
                pass

            self.task = None

        logger.info("Telemetry worker stopped")

    async def _ensure_consumer_group(self):
        try:
            await redis_client.xgroup_create(
                name=settings.telemetry_stream_key,
                groupname=CONSUMER_GROUP,
                id="$",
                mkstream=True,
            )

            logger.info("Redis telemetry consumer group created")

        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _process_messages(self, messages):
        for message_id, fields in messages:

            try:
                event_type = fields.get("event_type")

                if event_type == "vehicle_update":
                    raw_vehicle = fields.get("vehicle")

                    if not raw_vehicle:
                        logger.warning(
                            "Telemetry event %s has no vehicle payload",
                            message_id,
                        )
                        await redis_client.xack(
                            settings.telemetry_stream_key,
                            CONSUMER_GROUP,
                            message_id,
                        )
                        continue

                    vehicle = json.loads(raw_vehicle)

                    await manager.broadcast({
                        "type": "vehicle_update",
                        "vehicle": vehicle,
                    })

                await redis_client.xack(
                    settings.telemetry_stream_key,
                    CONSUMER_GROUP,
                    message_id,
                )

            except json.JSONDecodeError:
                logger.exception(
                    "Invalid vehicle JSON in telemetry event %s",
                    message_id,
                )

                await redis_client.xack(
                    settings.telemetry_stream_key,
                    CONSUMER_GROUP,
                    message_id,
                )

            except Exception:
                logger.exception(
                    "Failed processing telemetry event %s",
                    message_id,
                )
                raise

    async def _consume(self):
        while self.running:
            try:
                await self._ensure_consumer_group()

                # Recover messages previously delivered to this consumer
                # but not acknowledged before a restart.
                pending = await redis_client.xreadgroup(
                    groupname=CONSUMER_GROUP,
                    consumername=CONSUMER_NAME,
                    streams={settings.telemetry_stream_key: "0"},
                    count=BATCH_SIZE,
                )

                if pending:
                    for _, messages in pending:
                        await self._process_messages(messages)

                    continue

                # Read new telemetry events.
                response = await redis_client.xreadgroup(
                    groupname=CONSUMER_GROUP,
                    consumername=CONSUMER_NAME,
                    streams={settings.telemetry_stream_key: ">"},
                    count=BATCH_SIZE,
                    block=1000,
                )

                if response:
                    for _, messages in response:
                        await self._process_messages(messages)

            except asyncio.CancelledError:
                raise

            except Exception:
                logger.exception(
                    "Telemetry worker encountered an error. Retrying..."
                )
                await asyncio.sleep(3)


telemetry_worker = TelemetryWorker()