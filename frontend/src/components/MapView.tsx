import {
  Circle,
  MapContainer,
  TileLayer,
  Marker,
  Popup,
  ZoomControl,
  Polyline,
  useMap,
} from "react-leaflet";

import L from "leaflet";
import { useEffect } from "react";

import type {
  Geofence,
  RecoveryPlan,
  Vehicle,
} from "../types";


const pin = (color: string) =>
  L.divIcon({
    className: "vehicle-pin-wrap",
    html: `<div class="vehicle-pin ${color}"><span></span></div>`,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });


const point = (v?: Vehicle) =>
  v?.latitude != null && v.longitude != null
    ? [v.latitude, v.longitude] as [number, number]
    : null;


function FitSelection({
  vehicle,
}: {
  vehicle?: Vehicle;
}) {
  const map = useMap();

  useEffect(() => {
    const p = point(vehicle);

    if (p) {
      map.flyTo(p, 15, {
        duration: 0.6,
      });
    }
  }, [
    vehicle?.id,
    vehicle?.latitude,
    vehicle?.longitude,
    map,
  ]);

  return null;
}


export default function MapView({
  vehicles,
  geofences,
  selected,
  onSelect,
  recoveryPlans = [],
}: {
  vehicles: Vehicle[];
  geofences: Geofence[];
  selected?: Vehicle;
  onSelect: (v: Vehicle) => void;
  recoveryPlans?: RecoveryPlan[];
}) {
  const activePlans = recoveryPlans.filter(
    p =>
      ![
        "COMPLETED",
        "FAILED",
        "CANCELLED",
      ].includes((p.status || "").toUpperCase())
  );

  return (
    <MapContainer
      center={[22.7196, 75.8577]}
      zoom={12}
      zoomControl={false}
      className="map"
    >
      <ZoomControl position="topleft" />

      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />

      <FitSelection vehicle={selected} />

      {/* Geofence zones */}
      {geofences.map(g => (
        <Circle
          key={g.id}
          center={[g.latitude, g.longitude]}
          radius={g.radius_m}
          pathOptions={{
            color: "#22b8cf",
            weight: 1.5,
            fillColor: "#22b8cf",
            fillOpacity: 0.08,
          }}
        >
          <Popup>
            <b>{g.name}</b>
            <br />
            Radius: {g.radius_m} m
          </Popup>
        </Circle>
      ))}

      {/* Recovery plans and delivery destinations */}
      {activePlans.map(p => {
        const helper = vehicles.find(
          v => v.id === p.helper_vehicle_id
        );

        const incident = vehicles.find(
          v => v.id === p.delayed_vehicle_id
        );

        const helperPoint = point(helper);
        const incidentPoint = point(incident);

        const destinations = (
          p.plan?.destinations || []
        ).filter(
          d =>
            Number.isFinite(d.latitude) &&
            Number.isFinite(d.longitude)
        );

        const destinationPoints = destinations.map(
          d =>
            [
              d.latitude,
              d.longitude,
            ] as [number, number]
        );

        const route = [
          ...(helperPoint ? [helperPoint] : []),
          ...destinationPoints,
        ];

        const currentIndex =
          p.plan?.destination_index ?? 0;

        return (
          <span key={p.id}>
            {incidentPoint && (
              <Marker
                position={incidentPoint}
                icon={pin("red")}
              >
                <Popup>
                  <b>Incident: {p.delayed_vehicle_id}</b>
                  <br />
                  Recovery: {p.status}
                </Popup>
              </Marker>
            )}

            {helperPoint && (
              <Marker
                position={helperPoint}
                icon={pin("orange")}
              >
                <Popup>
                  <b>Recovery helper: {p.helper_vehicle_id}</b>
                  <br />
                  Status: {helper?.status || "Unknown"}
                  <br />
                  Speed: {helper?.speed ?? "—"} km/h
                </Popup>
              </Marker>
            )}

            {route.length > 1 && (
              <Polyline
                positions={route}
                pathOptions={{
                  color: "#e5a33b",
                  weight: 4,
                  opacity: 0.85,
                  dashArray: "8 7",
                }}
              />
            )}

            {destinations.map((d, index) => {
              const completed = index < currentIndex;

              return (
                <Marker
                  key={`${p.id}-${d.delivery_id}`}
                  position={[
                    d.latitude,
                    d.longitude,
                  ]}
                  icon={pin(
                    completed ? "green" : "orange"
                  )}
                >
                  <Popup>
                    <b>Delivery: {d.delivery_id}</b>
                    <br />
                    Status: {completed ? "DELIVERED" : d.status || "PENDING"}
                    <br />
                    Latitude: {d.latitude.toFixed(6)}
                    <br />
                    Longitude: {d.longitude.toFixed(6)}
                    <br />
                    Stop: {index + 1} of {destinations.length}
                  </Popup>
                </Marker>
              );
            })}
          </span>
        );
      })}

      {/* Fleet vehicles */}
      {vehicles.filter(v => point(v)).map(v => (
        <Marker
          key={v.id}
          position={point(v)!}
          icon={pin(
            v.status === "BREAKDOWN"
              ? "red"
              : v.status === "DELAYED" ||
                  v.status === "STOPPED"
                ? "orange"
                : "green"
          )}
          eventHandlers={{
            click: () => onSelect(v),
          }}
        >
          <Popup>
            <b>{v.id}</b>
            <br />
            Status: {v.status}
            <br />
            Speed: {v.speed ?? "—"} km/h
          </Popup>
        </Marker>
      ))}
    </MapContainer>
  );
}