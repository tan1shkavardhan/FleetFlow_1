# FleetFlow

Real-time geospatial fleet monitoring and incident recovery using FastAPI, PostgreSQL, Redis, React, TypeScript, Vite and Leaflet.

## Included workflow

- Live vehicle telemetry ingestion, Redis Geo latest locations, PostgreSQL telemetry history and dashboard WebSocket updates.
- Configurable delay detection and five-minute stationary breakdown detection for configured demo vehicles.
- Geofence entry/exit alerts.
- Recovery candidate analysis using existing cargo packing and route-feasibility logic.
- GPS-driven helper arrival, cargo handover, delivery completion and persisted recovery history.
- REST alert history and live alert updates.

The route optimizer uses straight-line distance estimates; it is not road-network navigation. Recovery handover in the current demo is simulated by the backend lifecycle timer after GPS-proximity arrival. Do not interpret that timer as a real vehicle-to-vehicle acknowledgement protocol.

## Requirements

- macOS/Linux (commands below use a POSIX shell)
- Python 3.11+ (3.14 was used for local checks)
- Node.js 20+ and npm
- Docker Desktop / Docker Engine with Compose

## 1. Configure environment

From the project root:

```bash
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env
```

The backend example uses PostgreSQL host port `1543` to match `docker-compose.yml`, and Redis host port `6379`. The demo breakdown vehicle is `truck_002`; the stationary threshold is `300` seconds. Preserve your own database credentials and other local settings if you already have an `.env` file. Never commit `.env` files.

## 2. Start PostgreSQL and Redis

```bash
docker compose up -d
docker compose ps
docker compose exec postgres pg_isready -U fleetflow -d fleetflow
docker compose exec redis redis-cli ping
```

Expected service checks: PostgreSQL `accepting connections`; Redis `PONG`.

## 3. Start the backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=. python -m scripts.seed_demo
PYTHONPATH=. uvicorn app.main:app --reload
```

API: `http://127.0.0.1:8000`  
Swagger: `http://127.0.0.1:8000/docs`  
Health: `http://127.0.0.1:8000/health`

Keep this terminal running.

## 4. Start the frontend

In a second terminal:

```bash
cd frontend
npm ci
npm run dev
```

Open the Vite URL printed in the terminal (normally `http://localhost:5173`).

## 5. Start one simulator process

In a third terminal:

```bash
cd backend
source venv/bin/activate
PYTHONPATH=. python simulator/driver_simulator.py --vehicles 10 --interval 2
```

The default demo scenarios are `truck_002` (stationary breakdown candidate) and `truck_003` (slow-speed delay). The simulator creates unique event IDs per run so retries can be deduplicated. **Run only one simulator process for a given demo fleet.** Starting multiple copies sends conflicting telemetry for the same vehicle IDs and creates artificial geofence transitions.

To check for duplicate simulator processes on macOS/Linux:

```bash
pgrep -fl 'driver_simulator.py'
```

Stop a simulator with `Ctrl+C`.

## 6. Verification

Backend tests:

```bash
cd backend
source venv/bin/activate
PYTHONPATH=. pytest -q
```

Frontend production build:

```bash
cd frontend
npm run build
```

Telemetry accepts an optional `event_id` and rejects duplicate IDs and stale/out-of-order timestamps for the same vehicle. Responses include `accepted: false` and a reason when an event is ignored.

## Important notes

- `backend/.env` and `frontend/.env` are local configuration files and are intentionally not included in the source archive. Copy the examples and adjust values for your machine.
- `node_modules`, Python virtual environments, caches and generated frontend `dist` output are intentionally excluded. Use `npm ci` and `pip install -r backend/requirements.txt` to restore dependencies.
- Database tables are created on backend startup for this local demo. A production deployment should use versioned database migrations, managed secrets, authenticated operator/device identities, TLS, monitoring and backups.
- Vehicle-to-vehicle messaging with durable message inbox/outbox, authenticated per-vehicle sessions, retries, hazard distribution and communication UI is not represented as complete in this baseline. Those items are described as later phases in the supplied product specification and must not be mistaken for implemented functionality.
