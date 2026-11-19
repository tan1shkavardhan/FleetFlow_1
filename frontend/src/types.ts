export type Vehicle = {
  id: string;
  name?: string;
  vehicle_type?: string;
  status?: string;
  active?: boolean;
  capacity_kg?: number;
  max_payload_kg?: number;
  latitude?: number;
  longitude?: number;
  speed?: number;
  heading?: number | null;
  timestamp?: string;
  last_update?: string;
  movement_classification?: string;
};

export type Geofence = {
  id: string;
  name: string;
  latitude: number;
  longitude: number;
  radius_m: number;
  active: boolean;
  demo_seed?: boolean;
};

export type FleetAlert = {
  id?: string;
  alert_id?: string;
  vehicle_id: string;
  type: string;
  message?: string;
  details?: Record<string, any>;
  timestamp: string;
};

export type RecoveryDestination = {
  delivery_id: string;
  latitude: number;
  longitude: number;
  status?: string;
};

export type RecoveryHistoryItem = {
  previous_status?: string | null;
  status: string;
  notes?: string;
  simulated?: boolean;
  timestamp: string;
};

export type RecoveryPlan = {
  id: string;
  delayed_vehicle_id: string;
  helper_vehicle_id?: string;
  status: string;
  created_at: string;
  radius_km?: number;
  plan?: {
    package_ids?: string[];
    delivery_ids?: string[];
    cargo_transfer_status?: string;
    destinations?: RecoveryDestination[];
    destination_index?: number;
    [key: string]: any;
  };
};

export type CargoDelivery = {
  id: string;
  customer_name?: string;
  latitude: number;
  longitude: number;
  status: string;
  priority?: string;
};

export type CargoPackage = {
  id: string;
  weight_kg: number;
  dimensions_cm: number[];
  delivery_id?: string | null;
  status: string;
  delivery?: CargoDelivery | null;
};

export type VehicleCargo = {
  vehicle_id: string;
  payload_capacity_kg: number;
  assigned_weight_kg: number;
  remaining_payload_kg: number;
  packages: CargoPackage[];
};