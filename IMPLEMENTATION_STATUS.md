# FleetFlow implementation and verification status

This archive is a clean source package based on the supplied repository and the supplied FleetFlow specification. It intentionally excludes local secrets, virtual environments, `node_modules`, caches and generated build output.

## Changes in this package

- Set the default demo breakdown vehicle to `truck_002` and the default stationary threshold to the specification's 300 seconds.
- Aligned the backend example database URL with the PostgreSQL host port (`1543`) published by Docker Compose.
- Added optional telemetry `event_id` support. The simulator now emits a unique event ID for each vehicle update; duplicate IDs are ignored for seven days.
- Reject stale/out-of-order telemetry for a vehicle before it can alter latest location or geofence state.
- Fixed Alert Center refresh ordering: REST and WebSocket alerts are deduplicated and sorted newest-first.
- Added Vite client type declarations and corrected TypeScript callback typing/compatibility in the existing UI.
- Updated the prolonged-stoppage test to use the configured demo vehicle and configured threshold, and to verify breakdown latching after movement.
- Updated setup instructions for macOS/Linux and documented the one-simulator-process rule.

## Checks completed in this build environment

- `PYTHONPATH=backend python3 -m pytest -q backend/tests`: **9 passed**.
- `python3 -m compileall -q backend/app backend/tests backend/simulator`: **passed**.
- `frontend/node_modules/.bin/tsc -b`: **passed**.
- Vite production bundling could not be run in this Linux build environment because the supplied dependency tree contains the macOS Rollup native optional dependency, not `@rollup/rollup-linux-x64-gnu`. The frontend production build was previously confirmed on the user's macOS machine; rerun `npm ci && npm run build` on macOS after extracting this archive.
- PostgreSQL/Redis and browser end-to-end checks require the user's local Docker services and browser and are not represented as rerun in this build environment.

## Specification items that remain outside the verified implementation

The current repository still does not implement the full backend-mediated V2V communications service described in the specification: authenticated per-vehicle sessions, durable message inbox/outbox, delivery/acknowledgement/retry/expiry processing, road-hazard distribution, and a dedicated Vehicle Communications dashboard. The current recovery simulator drives helper movement by reading persisted recovery plans and the backend's recovery lifecycle advances handover/delivery from GPS; this should not be misrepresented as a complete authenticated V2V protocol.

Production hardening and scale phases in the specification (operator/device authentication and roles, migrations, TLS deployment, monitoring/backups, load testing and 100–1000 vehicle performance) are also not complete in this local demo repository.

No unrelated features were added. See `README.md` for setup and run instructions.
