-- Run once against fleetflow DB. Keep the newer van_008 plan because current
-- package/delivery ownership is already van_008. Retire the older van_009 plan.
BEGIN;
UPDATE recovery_plans
SET status = 'CANCELLED', executed_at = COALESCE(executed_at, NOW())
WHERE id = 'recovery_e6cf65846a32'
  AND delayed_vehicle_id = 'truck_006'
  AND helper_vehicle_id = 'van_009'
  AND status = 'ASSIGNED';

INSERT INTO recovery_status_history
(id, recovery_plan_id, previous_status, new_status, notes, simulated, changed_at)
SELECT 'rsh_' || substr(md5(random()::text || clock_timestamp()::text), 1, 12),
       'recovery_e6cf65846a32', 'ASSIGNED', 'CANCELLED',
       'Retired duplicate legacy plan; newer van_008 plan retained.', TRUE, NOW()
WHERE EXISTS (
  SELECT 1 FROM recovery_plans
  WHERE id = 'recovery_e6cf65846a32' AND status = 'CANCELLED'
)
AND NOT EXISTS (
  SELECT 1 FROM recovery_status_history
  WHERE recovery_plan_id = 'recovery_e6cf65846a32' AND new_status = 'CANCELLED'
);
COMMIT;
