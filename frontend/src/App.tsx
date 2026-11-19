
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Bell,
  CheckCircle2,
  CircleDot,
  Clock3,
  Command,
  Crosshair,
  MapPin,
  Radio,
  RefreshCw,
  Route,
  Truck,
  X,
} from "lucide-react";

import MapView from "./components/MapView";

import {
  analyzeRecovery,
  errorMessage,
  executeRecovery,
  getAlerts,
  getGeofences,
  getHealth,
  getHistory,
  getRecoveryHistory,
  getRecoveryPlans,
  getVehicles,
  WS_URL,
} from "./services/api";

import type {
  FleetAlert,
  Geofence,
  RecoveryHistoryItem,
  RecoveryPlan,
  Vehicle,
} from "./types";


const fmt = (v?: string) =>
  v ? new Date(v).toLocaleString() : "—";

const terminal = [
  "COMPLETED",
  "FAILED",
  "CANCELLED",
];

const lifecycle = [
  "ASSIGNED",
  "EN_ROUTE_TO_INCIDENT",
  "ARRIVED_AT_INCIDENT",
  "HANDOVER_IN_PROGRESS",
  "HANDOVER_COMPLETED",
  "IN_TRANSIT_TO_DESTINATION",
  "COMPLETED",
];

const activeRecoveryStatuses = [
  "ASSIGNED",
  "EN_ROUTE_TO_INCIDENT",
  "ARRIVED_AT_INCIDENT",
  "HANDOVER_IN_PROGRESS",
  "HANDOVER_COMPLETED",
  "IN_TRANSIT_TO_DESTINATION",
];

const label = (s: string) =>
  (s || "UNKNOWN").replace(/_/g, " ");

const isDone = (s: string) =>
  terminal.includes((s || "").toUpperCase());


export default function App() {
  const [vehicles, setVehicles] = useState<Vehicle[]>([]);
  const [geofences, setGeofences] = useState<Geofence[]>([]);
  const [alerts, setAlerts] = useState<FleetAlert[]>([]);
  const [plans, setPlans] = useState<RecoveryPlan[]>([]);

  const [selectedId, setSelectedId] = useState("");
  const [history, setHistory] = useState<any[]>([]);

  const [recoveryHistory, setRecoveryHistory] = useState<
    Record<string, RecoveryHistoryItem[]>
  >({});

  const [selectedPlanId, setSelectedPlanId] = useState("");
  const [health, setHealth] = useState<any>(null);

  const [ws, setWs] = useState<
    "connecting" | "connected" | "disconnected"
  >("connecting");

  const [lastUpdate, setLastUpdate] = useState("");
  const [tab, setTab] = useState("overview");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [recoveryTarget, setRecoveryTarget] = useState("");
  const [analysis, setAnalysis] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  const wsRef = useRef<WebSocket | null>(null);
  const reconnect = useRef<number>();


  const mergeVehicle = useCallback((incoming: any) => {
    const id = incoming.vehicle_id || incoming.id;

    if (!id) return;

    setVehicles(prev => {
      const old = prev.find(v => v.id === id);

      const incomingTime = incoming.timestamp
        ? Date.parse(incoming.timestamp)
        : 0;

      const oldTime = old?.timestamp
        ? Date.parse(old.timestamp)
        : 0;

      if (
        old &&
        incomingTime &&
        oldTime &&
        incomingTime < oldTime
      ) {
        return prev;
      }

      const next = {
        ...(old || {
          id,
          status: "MOVING",
          active: true,
        }),
        ...incoming,
        id,
        latitude: incoming.latitude ?? old?.latitude,
        longitude: incoming.longitude ?? old?.longitude,
        timestamp:
          incoming.timestamp ||
          old?.timestamp ||
          new Date().toISOString(),
      };

      return old
        ? prev.map(v => v.id === id ? next : v)
        : [...prev, next];
    });

    setLastUpdate(new Date().toISOString());
  }, []);


  const addAlert = useCallback((a: any) => {
    const item: FleetAlert = {
      ...a,
      id:
        a.alert_id ||
        a.id ||
        `${a.vehicle_id}-${a.type}-${a.timestamp}`,
      type: a.type || "event",
      timestamp: a.timestamp || new Date().toISOString(),
    };

    setAlerts(prev =>
      [
        item,
        ...prev.filter(x => x.id !== item.id),
      ]
        .sort(
          (a, b) =>
            Date.parse(b.timestamp || "") -
            Date.parse(a.timestamp || "")
        )
        .slice(0, 100)
    );
  }, []);


  const refresh = useCallback(async () => {
    setError("");

    try {
      const [
        vs,
        gs,
        as,
        ps,
        h,
      ] = await Promise.all([
        getVehicles(),
        getGeofences(),
        getAlerts(),
        getRecoveryPlans(),
        getHealth(),
      ]);

      setVehicles(prev => {
        const live = new Map(
          prev.map(v => [v.id, v])
        );

        return (vs || []).map((v: Vehicle) => {
          const old = live.get(v.id);

          return old &&
            Date.parse(old.timestamp || "") >
              Date.parse(v.timestamp || "")
            ? { ...v, ...old }
            : v;
        });
      });

      setGeofences(gs || []);

      setAlerts(old => {
        const combined = [
          ...(as || []).map((a: any) => ({
            ...a,
            id: a.id || a.alert_id,
          })),
          ...old,
        ];

        const unique = combined.filter(
          (a, i) =>
            combined.findIndex(b => b.id === a.id) === i
        );

        return unique
          .sort(
            (a, b) =>
              Date.parse(b.timestamp || "") -
              Date.parse(a.timestamp || "")
          )
          .slice(0, 100);
      });

      setPlans(ps || []);
      setHealth(h);
      setLastUpdate(new Date().toISOString());

    } catch (e) {
      setError(errorMessage(e));

    } finally {
      setLoading(false);
    }
  }, []);


  const refreshRecoveryHistory = useCallback(
    async (items: RecoveryPlan[]) => {
      const activeIds = items.map(p => p.id);

      const results = await Promise.allSettled(
        items.map(p => getRecoveryHistory(p.id))
      );

      const next: Record<string, RecoveryHistoryItem[]> = {};

      results.forEach((r, i) => {
        if (r.status === "fulfilled") {
          next[activeIds[i]] = r.value.history || [];
        }
      });

      setRecoveryHistory(next);
    },
    []
  );


  useEffect(() => {
    refresh();

    const id = window.setInterval(
      refresh,
      5000
    );

    return () => clearInterval(id);
  }, [refresh]);


  useEffect(() => {
    if (plans.length) {
      refreshRecoveryHistory(plans);
    } else {
      setRecoveryHistory({});
    }
  }, [plans, refreshRecoveryHistory]);


  useEffect(() => {
    let alive = true;
    let delay = 1500;

    const connect = () => {
      if (!alive) return;

      setWs("connecting");

      const socket = new WebSocket(WS_URL);
      wsRef.current = socket;

      socket.onopen = () => {
        delay = 1500;
        setWs("connected");
      };

      socket.onmessage = ev => {
        try {
          const msg = JSON.parse(ev.data);

          if (
            msg.type === "vehicle_update" &&
            msg.vehicle
          ) {
            mergeVehicle(msg.vehicle);
            return;
          }

          if (
            [
              "breakdown_alert",
              "delay_alert",
              "geofence_alert",
              "delay_recovered",
            ].includes(msg.type)
          ) {
            addAlert(msg);
            return;
          }

          if (msg.type?.startsWith("recovery_")) {
            if (
              msg.type === "recovery_failed" ||
              msg.type === "recovery_unavailable"
            ) {
              addAlert({
                ...msg,
                vehicle_id: msg.delayed_vehicle_id,
                message:
                  msg.message ||
                  "Recovery is waiting for a feasible helper.",
              });
            }

            if (msg.message || msg.status) {
              setNotice(
                msg.message ||
                `Recovery ${msg.recovery_plan_id}: ${label(msg.status)}`
              );
            }

            refresh();
            return;
          }

        } catch (e) {
          console.error(
            "WebSocket message error",
            e
          );
        }
      };

      socket.onclose = () => {
        if (!alive) return;

        setWs("disconnected");

        reconnect.current = window.setTimeout(
          connect,
          delay
        );

        delay = Math.min(
          delay * 1.7,
          10000
        );
      };

      socket.onerror = () => socket.close();
    };

    connect();

    return () => {
      alive = false;

      if (reconnect.current) {
        clearTimeout(reconnect.current);
      }

      wsRef.current?.close();
    };
  }, [addAlert, mergeVehicle, refresh]);


  useEffect(() => {
    if (selectedId) {
      getHistory(selectedId)
        .then(r => setHistory(r.history || []))
        .catch(() => setHistory([]));
    } else {
      setHistory([]);
    }
  }, [selectedId]);


  const selected = vehicles.find(
    v => v.id === selectedId
  );

  const filtered = useMemo(
    () =>
      vehicles.filter(v =>
        v.id.toLowerCase().includes(query.toLowerCase())
      ),
    [vehicles, query]
  );


  const counts = {
    moving: vehicles.filter(
      v =>
        ![
          "BREAKDOWN",
          "DELAYED",
          "STOPPED",
          "INACTIVE",
        ].includes((v.status || "").toUpperCase())
    ).length,

    breakdown: vehicles.filter(
      v =>
        (v.status || "").toUpperCase() === "BREAKDOWN"
    ).length,

    delayed: vehicles.filter(
      v =>
        ["DELAYED", "STOPPED"].includes(
          (v.status || "").toUpperCase()
        )
    ).length,
  };


  // -------------------------------------------------------
  // INCIDENT BOARD DATA
  // -------------------------------------------------------

  const incidents = useMemo(() => {
    const affected = vehicles.filter(v =>
      [
        "BREAKDOWN",
        "DELAYED",
        "STOPPED",
      ].includes((v.status || "").toUpperCase())
    );

    return affected.map(vehicle => {
      const vehiclePlans = plans
        .filter(
          p =>
            p.delayed_vehicle_id === vehicle.id
        )
        .sort(
          (a, b) =>
            Date.parse(b.created_at || "") -
            Date.parse(a.created_at || "")
        );

      const activePlan = vehiclePlans.find(
        p =>
          activeRecoveryStatuses.includes(
            (p.status || "").toUpperCase()
          )
      );

      const latestPlan = vehiclePlans[0];

      let incidentState = "WAITING_FOR_HELPER";

      if (activePlan) {
        incidentState = "RECOVERY_ACTIVE";
      } else if (
        latestPlan &&
        isDone(latestPlan.status)
      ) {
        incidentState = latestPlan.status.toUpperCase();
      } else if (
        (vehicle.status || "").toUpperCase() !== "BREAKDOWN"
      ) {
        incidentState = "DISRUPTION_REQUIRES_REVIEW";
      }

      const latestAlert = alerts.find(
        a =>
          a.vehicle_id === vehicle.id &&
          [
            "breakdown_alert",
            "recovery_unavailable",
            "recovery_failed",
          ].includes(a.type || "")
      );

      return {
        vehicle,
        activePlan,
        latestPlan,
        incidentState,
        latestAlert,
      };
    });
  }, [vehicles, plans, alerts]);


  const breakdownIncidents = incidents.filter(
    item =>
      (item.vehicle.status || "").toUpperCase() === "BREAKDOWN"
  );

  const waitingIncidents = breakdownIncidents.filter(
    item => !item.activePlan
  );

  const doAnalyze = async () => {
    if (!recoveryTarget) {
      setNotice(
        "Select a delayed or breakdown vehicle first."
      );
      return;
    }

    setBusy(true);
    setAnalysis(null);

    try {
      const result = await analyzeRecovery({
        delayed_vehicle_id: recoveryTarget,
        package_ids: [],
        delivery_ids: [],
        radius_km: 20,
        max_candidates: 10,
        apply_accessibility: true,
      });

      setAnalysis(result);

      setNotice(
        "Recovery analysis completed. Review feasible helper candidates."
      );

    } catch (e) {
      setNotice(
        `Recovery analysis failed: ${errorMessage(e)}`
      );

    } finally {
      setBusy(false);
    }
  };


  const doExecute = async (candidate: any) => {
    if (!analysis?.analysis_id) return;

    setBusy(true);

    try {
      const result = await executeRecovery(
        {
          helper_vehicle_id: candidate.vehicle_id,
          package_ids: analysis.package_ids || [],
          delivery_ids: analysis.delivery_ids || [],
        },
        analysis.analysis_id,
        recoveryTarget
      );

      setNotice(
        `Recovery plan ${result.recovery_plan_id} assigned.`
      );

      setAnalysis(null);
      await refresh();

    } catch (e) {
      setNotice(
        `Assignment failed: ${errorMessage(e)}`
      );

    } finally {
      setBusy(false);
    }
  };


  const selectIncident = (vehicleId: string) => {
    setRecoveryTarget(vehicleId);
    setSelectedId(vehicleId);
    setAnalysis(null);
  };


  const selectedPlan =
    plans.find(p => p.id === selectedPlanId) ||
    plans[0];

  const selectedHistory = selectedPlan
    ? recoveryHistory[selectedPlan.id] || []
    : [];


  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <Command size={21} />
          </div>

          <div>
            <b>FLEETFLOW</b>
            <small>OPERATIONS PLATFORM</small>
          </div>
        </div>

        <div className="nav-label">WORKSPACE</div>

        {(
          [
            ["overview", "Fleet overview", Activity],
            ["tracking", "Live tracking", MapPin],
            ["alerts", "Alert center", Bell],
            ["recovery", "Recovery operations", Route],
            ["geofences", "Geofences", Crosshair],
          ] as any[]
        ).map(([key, title, Icon]) => (
          <button
            key={key}
            className={`nav-item ${tab === key ? "active" : ""}`}
            onClick={() => setTab(key)}
          >
            <Icon size={17} />
            <span>{title}</span>

            {key === "alerts" && alerts.length > 0 && (
              <i>{alerts.length}</i>
            )}
          </button>
        ))}

        <div className="sidebar-bottom">
          <div className="connection-line">
            <span
              className={`dot ${
                health?.status === "healthy"
                  ? "green"
                  : "amber"
              }`}
            />
            API {health?.status || "checking"}
          </div>

          <div className="connection-line">
            <span
              className={`dot ${
                ws === "connected"
                  ? "green"
                  : "amber"
              }`}
            />
            WebSocket {ws}
          </div>

          <small>FleetFlow • Local deployment</small>
        </div>
      </aside>


      <main className="main">
        <header className="topbar">
          <div>
            <div className="eyebrow">
              FLEET INTELLIGENCE / {tab.toUpperCase()}
            </div>

            <h1>
              {({
                overview: "Command center",
                tracking: "Live tracking",
                alerts: "Alert center",
                recovery: "Recovery operations",
                geofences: "Geofence monitor",
              } as any)[tab]}
            </h1>
          </div>

          <div className="top-actions">
            <div
              className={`live-pill ${
                ws === "connected" ? "is-live" : ""
              }`}
            >
              <span />
              {ws === "connected"
                ? "LIVE STREAM"
                : ws.toUpperCase()}
            </div>

            <button
              className="icon-btn"
              onClick={refresh}
              title="Refresh data"
            >
              <RefreshCw size={17} />
            </button>

            <div className="avatar">FF</div>
          </div>
        </header>


        {error && (
          <div className="error-banner">
            <AlertTriangle size={16} />
            <span>Backend request failed: {error}</span>
            <button onClick={() => setError("")}>
              <X size={15} />
            </button>
          </div>
        )}

        {notice && (
          <div className="notice">
            <span>{notice}</span>
            <button onClick={() => setNotice("")}>
              <X size={15} />
            </button>
          </div>
        )}


        {/* ------------------------------------------------ */}
        {/* OVERVIEW                                         */}
        {/* ------------------------------------------------ */}

        {tab === "overview" && (
          <>
            <section className="metric-grid">
              <Metric
                icon={Truck}
                label="Tracked vehicles"
                value={vehicles.length}
                tone="blue"
                sub="Registered fleet"
              />

              <Metric
                icon={Activity}
                label="Moving"
                value={counts.moving}
                tone="green"
                sub="Active telemetry"
              />

              <Metric
                icon={AlertTriangle}
                label="Breakdowns"
                value={counts.breakdown}
                tone="red"
                sub="Requires attention"
              />

              <Metric
                icon={Clock3}
                label="Delays / stops"
                value={counts.delayed}
                tone="orange"
                sub="Movement anomalies"
              />
            </section>


            <section className="content-grid">
              <div className="panel map-panel">
                <PanelHead
                  eyebrow="GEOSPATIAL MONITOR"
                  title="Live fleet map"
                  right={
                    <span className="map-legend">
                      <i className="green-bg" />
                      Moving
                      <i className="red-bg" />
                      Breakdown
                      <i className="orange-bg" />
                      Delay
                    </span>
                  }
                />

                <MapView
                  vehicles={vehicles}
                  geofences={geofences}
                  selected={selected}
                  onSelect={v => setSelectedId(v.id)}
                  recoveryPlans={plans}
                />

                <div className="map-footer">
                  <span>
                    <MapPin size={13} />
                    {vehicles.filter(v => v.latitude != null).length}
                    {" "}located vehicles
                  </span>

                  <span>
                    <Crosshair size={13} />
                    {geofences.length} active geofences
                  </span>

                  <span>
                    <Radio size={13} />
                    {ws}
                  </span>
                </div>
              </div>


              <div className="right-stack">
                <div className="panel inventory-panel">
                  <PanelHead
                    eyebrow="FLEET INVENTORY"
                    title={`Vehicles · ${vehicles.length}`}
                    right={
                      <button
                        className="text-btn"
                        onClick={refresh}
                      >
                        Refresh
                      </button>
                    }
                  />

                  <input
                    className="search"
                    placeholder="Search vehicle ID"
                    value={query}
                    onChange={e => setQuery(e.target.value)}
                  />

                  <div className="vehicle-list">
                    {filtered.length ? (
                      filtered.map(v => (
                        <VehicleRow
                          key={v.id}
                          vehicle={v}
                          selected={selectedId === v.id}
                          onClick={() => setSelectedId(v.id)}
                        />
                      ))
                    ) : (
                      <Empty
                        text={
                          loading
                            ? "Loading vehicles…"
                            : "No vehicles registered."
                        }
                      />
                    )}
                  </div>
                </div>


                <div className="panel inspector">
                  <PanelHead
                    eyebrow="VEHICLE INSPECTOR"
                    title={selected?.id || "Selected vehicle"}
                  />

                  {selected ? (
                    <>
                      <div className="status-row">
                        <Status value={selected.status} />
                        <span className="muted">
                          {selected.vehicle_type || "Vehicle"}
                        </span>
                      </div>

                      <div className="detail-grid">
                        <Detail
                          label="Speed"
                          value={`${selected.speed ?? "—"} km/h`}
                        />

                        <Detail
                          label="Latitude"
                          value={selected.latitude?.toFixed(5) ?? "—"}
                        />

                        <Detail
                          label="Longitude"
                          value={selected.longitude?.toFixed(5) ?? "—"}
                        />

                        <Detail
                          label="Last update"
                          value={fmt(selected.timestamp)}
                        />
                      </div>

                      <div className="mini-history">
                        <b>Recent telemetry</b>

                        {history.slice(0, 4).map((h, i) => (
                          <div key={i}>
                            <span>{fmt(h.timestamp)}</span>
                            <strong>{h.speed} km/h</strong>
                          </div>
                        ))}
                      </div>
                    </>
                  ) : (
                    <Empty text="Select a vehicle from inventory or map." />
                  )}
                </div>
              </div>
            </section>


            <section className="panel alerts-preview">
              <PanelHead
                eyebrow="LIVE EVENT STREAM"
                title="Recent alerts"
                right={
                  <button
                    className="text-btn"
                    onClick={() => setTab("alerts")}
                  >
                    View all →
                  </button>
                }
              />

              <AlertList
                alerts={alerts.slice(0, 5)}
                onSelect={id => {
                  setSelectedId(id);
                  setTab("overview");
                }}
              />
            </section>
          </>
        )}


        {/* ------------------------------------------------ */}
        {/* TRACKING                                         */}
        {/* ------------------------------------------------ */}

        {tab === "tracking" && (
          <section className="panel full-map">
            <PanelHead
              eyebrow="REAL-TIME TELEMETRY"
              title="Vehicle tracking map"
            />

            <MapView
              vehicles={vehicles}
              geofences={geofences}
              selected={selected}
              onSelect={v => setSelectedId(v.id)}
              recoveryPlans={plans}
            />

            <div className="tracking-list">
              {vehicles.map(v => (
                <VehicleRow
                  key={v.id}
                  vehicle={v}
                  selected={selectedId === v.id}
                  onClick={() => setSelectedId(v.id)}
                />
              ))}
            </div>
          </section>
        )}


        {/* ------------------------------------------------ */}
        {/* ALERTS                                           */}
        {/* ------------------------------------------------ */}

        {tab === "alerts" && (
          <section className="panel page-panel">
            <PanelHead
              eyebrow="OPERATIONAL EVENTS"
              title={`Alert history · ${alerts.length}`}
            />

            <AlertList
              alerts={alerts}
              onSelect={id => {
                setSelectedId(id);
                setTab("overview");
              }}
            />
          </section>
        )}


        {/* ------------------------------------------------ */}
        {/* GEOFENCES                                        */}
        {/* ------------------------------------------------ */}

        {tab === "geofences" && (
          <section className="panel page-panel">
            <PanelHead
              eyebrow="GEOFENCE CONFIGURATION"
              title={`Configured zones · ${geofences.length}`}
            />

            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Latitude</th>
                    <th>Longitude</th>
                    <th>Radius</th>
                    <th>Status</th>
                    <th>Source</th>
                  </tr>
                </thead>

                <tbody>
                  {geofences.map(g => (
                    <tr key={g.id}>
                      <td>
                        <b>{g.name}</b>
                        <small>{g.id}</small>
                      </td>

                      <td>{g.latitude.toFixed(5)}</td>
                      <td>{g.longitude.toFixed(5)}</td>
                      <td>{g.radius_m.toLocaleString()} m</td>

                      <td>
                        <Status
                          value={g.active ? "ACTIVE" : "INACTIVE"}
                        />
                      </td>

                      <td>
                        {g.demo_seed ? "Demo seed" : "Configured"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {!geofences.length && (
                <Empty text="No geofences returned by the backend." />
              )}
            </div>
          </section>
        )}


        {/* ------------------------------------------------ */}
        {/* RECOVERY OPERATIONS                              */}
        {/* ------------------------------------------------ */}

        {tab === "recovery" && (
          <section className="recovery-layout">

            <div className="panel page-panel">
              <PanelHead
                eyebrow="INCIDENT MONITOR"
                title="Incident recovery board"
                right={
                  <button
                    className="text-btn"
                    onClick={refresh}
                  >
                    <RefreshCw size={13} /> Refresh
                  </button>
                }
              />

              <div className="recovery-stats">
                <div className="recovery-stat">
                  <small>Active breakdowns</small>
                  <b>{breakdownIncidents.length}</b>
                </div>

                <div className="recovery-stat">
                  <small>Recovery plans active</small>
                  <b>
                    {breakdownIncidents.filter(
                      item => item.activePlan
                    ).length}
                  </b>
                </div>

                <div className="recovery-stat">
                  <small>Waiting for helper</small>
                  <b>{waitingIncidents.length}</b>
                </div>

                <div className="recovery-stat">
                  <small>Saved recovery plans</small>
                  <b>{plans.length}</b>
                </div>
              </div>


              <div className="recovery-plans">
                {incidents.map(item => {
                  const vehicle = item.vehicle;
                  const plan = item.activePlan || item.latestPlan;
                  const status = item.incidentState;

                  const helper = item.activePlan
                    ? vehicles.find(
                        v => v.id === item.activePlan?.helper_vehicle_id
                      )
                    : undefined;

                  const statusText =
                    status === "RECOVERY_ACTIVE"
                      ? `Helper ${item.activePlan?.helper_vehicle_id}`
                      : status === "WAITING_FOR_HELPER"
                      ? "No feasible helper assigned yet"
                      : status === "COMPLETED"
                      ? "Latest recovery completed"
                      : status === "FAILED"
                      ? "Latest recovery failed"
                      : status === "CANCELLED"
                      ? "Latest recovery cancelled"
                      : "Disruption requires review";

                  return (
                    <div
                      className="plan-card"
                      key={vehicle.id}
                    >
                      <div className="plan-select">
                        <div className="plan-top">
                          <b>{vehicle.id}</b>
                          <Status value={status} />
                        </div>

                        <div className="plan-subline">
                          <span>
                            {vehicle.status}
                          </span>

                          <span>
                            {vehicle.speed ?? "—"} km/h
                          </span>
                        </div>

                        <div className="plan-subline">
                          <span>{statusText}</span>
                          <span>
                            {vehicle.latitude?.toFixed(4) ?? "—"},
                            {" "}
                            {vehicle.longitude?.toFixed(4) ?? "—"}
                          </span>
                        </div>

                        {plan && (
                          <div className="plan-subline">
                            <span>
                              Plan: {plan.id}
                            </span>

                            <span>
                              {label(plan.status)}
                            </span>
                          </div>
                        )}

                        {helper && (
                          <div className="plan-subline">
                            <span>
                              Helper: {helper.id}
                            </span>

                            <span>
                              {helper.status}
                            </span>
                          </div>
                        )}

                        {item.latestAlert && (
                          <p className="recovery-note">
                            {item.latestAlert.message}
                          </p>
                        )}

                        <div className="plan-actions">
                          <button
                            className="small-btn"
                            onClick={() => selectIncident(vehicle.id)}
                          >
                            Analyze this incident
                          </button>

                          {plan && (
                            <button
                              className="text-btn"
                              onClick={() => {
                                setSelectedPlanId(plan.id);
                              }}
                            >
                              View plan
                            </button>
                          )}
                        </div>
                      </div>
                    </div>
                  );
                })}

                {!incidents.length && (
                  <Empty text="No active breakdowns, delays or stopped vehicles." />
                )}
              </div>

              <p className="recovery-note">
                Waiting incidents remain visible even when no feasible
                helper is available. The backend is responsible for
                retrying automatic recovery.
              </p>
            </div>


            {/* MANUAL ANALYSIS */}

            <div className="panel page-panel">
              <PanelHead
                eyebrow="DISRUPTION RESPONSE"
                title="Analyze helper vehicle"
              />

              <p className="muted intro">
                Evaluate nearby vehicles using backend cargo capacity,
                packing feasibility and route estimates.
              </p>

              <label className="field-label">
                Affected vehicle
              </label>

              <select
                className="select"
                value={recoveryTarget}
                onChange={e => {
                  setRecoveryTarget(e.target.value);
                  setAnalysis(null);
                }}
              >
                <option value="">
                  Select breakdown / delayed vehicle
                </option>

                {vehicles
                  .filter(v =>
                    [
                      "BREAKDOWN",
                      "DELAYED",
                      "STOPPED",
                    ].includes((v.status || "").toUpperCase())
                  )
                  .map(v => (
                    <option
                      key={v.id}
                      value={v.id}
                    >
                      {v.id} — {v.status}
                    </option>
                  ))}
              </select>

              <button
                className="primary-btn"
                disabled={busy || !recoveryTarget}
                onClick={doAnalyze}
              >
                {busy
                  ? "Analyzing…"
                  : "Analyze recovery candidates"}
              </button>


              {analysis && (
                <div className="analysis-results">
                  <div className="analysis-summary">
                    <b>
                      {analysis.candidate_count} candidates evaluated
                    </b>

                    <span>
                      Radius {analysis.radius_km} km
                    </span>
                  </div>

                  {(analysis.candidates || []).map(
                    (c: any) => (
                      <div
                        className="candidate"
                        key={c.vehicle_id}
                      >
                        <div>
                          <b>{c.vehicle_id}</b>

                          <Status
                            value={
                              c.feasible
                                ? "FEASIBLE"
                                : "UNSUITABLE"
                            }
                          />

                          <small>
                            {c.distance_km} km away ·
                            {" "}
                            {c.rejection_reason ||
                              c.recommendation ||
                              "Evaluated"}
                          </small>
                        </div>

                        <button
                          className="small-btn"
                          disabled={!c.feasible || busy}
                          onClick={() => doExecute(c)}
                        >
                          Assign helper
                        </button>
                      </div>
                    )
                  )}

                  {!analysis.candidates?.length && (
                    <Empty text="No candidates were returned." />
                  )}
                </div>
              )}

              <p className="recovery-note">
                Lifecycle progress is read from persisted backend
                history. This screen does not manually advance
                recovery states.
              </p>
            </div>


            {/* SAVED RECOVERY PLANS */}

            <div className="panel page-panel">
              <PanelHead
                eyebrow="RECOVERY LIFECYCLE"
                title={`Saved plans · ${plans.length}`}
                right={
                  <button
                    className="text-btn"
                    onClick={refresh}
                  >
                    Refresh
                  </button>
                }
              />

              <div className="recovery-plans">
                {plans.map(p => {
                  const h = recoveryHistory[p.id] || [];

                  const current = (
                    p.status || "ASSIGNED"
                  ).toUpperCase();

                  const idx = lifecycle.indexOf(current);

                  const progress =
                    current === "COMPLETED"
                      ? 100
                      : idx >= 0
                      ? Math.round(
                          (idx / (lifecycle.length - 1)) * 100
                        )
                      : 0;

                  const helper = vehicles.find(
                    v => v.id === p.helper_vehicle_id
                  );

                  return (
                    <div
                      className={`plan-card ${
                        selectedPlanId === p.id
                          ? "selected"
                          : ""
                      }`}
                      key={p.id}
                    >
                      <button
                        className="plan-select"
                        onClick={() => setSelectedPlanId(p.id)}
                      >
                        <div className="plan-top">
                          <b>
                            {p.delayed_vehicle_id}
                            {" → "}
                            {p.helper_vehicle_id || "Unassigned"}
                          </b>

                          <Status value={current} />
                        </div>

                        <div className="plan-subline">
                          <span>{p.id}</span>
                          <span>
                            Created {fmt(p.created_at)}
                          </span>
                        </div>

                        <div className="plan-progress">
                          <span
                            style={{
                              width: `${progress}%`,
                            }}
                          />
                        </div>

                        <div className="plan-subline">
                          <span>
                            Helper: {helper?.status || "No live status"}
                          </span>

                          <span>
                            {h.length} recorded events
                          </span>
                        </div>
                      </button>
                    </div>
                  );
                })}

                {!plans.length && (
                  <Empty text="No recovery plans yet. Automatic recovery plans will appear here." />
                )}
              </div>


              {selectedPlan && (
                <div className="recovery-detail">
                  <h3>
                    Recovery timeline · {selectedPlan.id}
                  </h3>

                  <div className="recovery-stats">
                    <div className="recovery-stat">
                      <small>Incident vehicle</small>
                      <b>{selectedPlan.delayed_vehicle_id}</b>
                    </div>

                    <div className="recovery-stat">
                      <small>Helper vehicle</small>
                      <b>
                        {selectedPlan.helper_vehicle_id || "—"}
                      </b>
                    </div>

                    <div className="recovery-stat">
                      <small>Helper GPS</small>
                      <b>
                        {vehicles.find(
                          v => v.id === selectedPlan.helper_vehicle_id
                        )?.latitude?.toFixed(5) ?? "—"},
                        {" "}
                        {vehicles.find(
                          v => v.id === selectedPlan.helper_vehicle_id
                        )?.longitude?.toFixed(5) ?? "—"}
                      </b>
                    </div>

                    <div className="recovery-stat">
                      <small>Helper speed</small>
                      <b>
                        {vehicles.find(
                          v => v.id === selectedPlan.helper_vehicle_id
                        )?.speed ?? "—"} km/h
                      </b>
                    </div>

                    <div className="recovery-stat">
                      <small>Cargo handover</small>
                      <b>
                        {selectedPlan.plan?.cargo_transfer_status ||
                          "See lifecycle history"}
                      </b>
                    </div>

                    <div className="recovery-stat">
                      <small>Plan status</small>
                      <b>{label(selectedPlan.status)}</b>
                    </div>
                  </div>


                  <h3>Recorded events</h3>

                  <div className="recovery-timeline">
                    {selectedHistory.length ? (
                      selectedHistory.map((item, i) => (
                        <div
                          className={`timeline-item ${
                            i === selectedHistory.length - 1
                              ? "latest"
                              : ""
                          }`}
                          key={`${item.timestamp}-${i}`}
                        >
                          <span className="timeline-dot" />

                          <div className="timeline-copy">
                            <b>{label(item.status)}</b>

                            <small>
                              {item.notes ||
                                "Status recorded by backend"}
                              <br />
                              {fmt(item.timestamp)}
                            </small>

                            {item.simulated && (
                              <em>SIMULATED TRANSITION</em>
                            )}
                          </div>
                        </div>
                      ))
                    ) : (
                      <Empty text="No lifecycle history has been returned for this plan yet." />
                    )}
                  </div>


                  {selectedPlan.plan?.package_ids?.length ? (
                    <>
                      <h3>Packages</h3>

                      <div className="cargo-list">
                        {selectedPlan.plan.package_ids.map(
                          (id: string) => (
                            <span
                              className="cargo-chip"
                              key={id}
                            >
                              {id}
                            </span>
                          )
                        )}
                      </div>
                    </>
                  ) : null}


                  {selectedPlan.plan?.delivery_ids?.length ? (
                    <>
                      <h3>Deliveries</h3>

                      <div className="cargo-list">
                        {selectedPlan.plan.delivery_ids.map(
                          (id: string) => (
                            <span
                              className="cargo-chip"
                              key={id}
                            >
                              {id}
                            </span>
                          )
                        )}
                      </div>
                    </>
                  ) : null}


                  {isDone(selectedPlan.status) && (
                    <p className="recovery-note">
                      <CheckCircle2 size={13} />
                      Backend reports this recovery as
                      {" "}
                      {label(selectedPlan.status)}.
                    </p>
                  )}
                </div>
              )}
            </div>
          </section>
        )}


        <footer className="footer">
          <span>FLEETFLOW COMMAND CENTER</span>

          <span>
            Last refreshed: {fmt(lastUpdate)}
            {" · "}
            API {health?.status || "unknown"}
            {" · "}
            <span
              className={
                ws === "connected"
                  ? "success-text"
                  : "warn-text"
              }
            >
              WS {ws}
            </span>
          </span>
        </footer>
      </main>
    </div>
  );
}


// ---------------------------------------------------------
// SHARED COMPONENTS
// ---------------------------------------------------------

function Metric({
  icon: Icon,
  label,
  value,
  tone,
  sub,
}: any) {
  return (
    <div className="metric-card">
      <div className={`metric-icon ${tone}`}>
        <Icon size={19} />
      </div>

      <div>
        <span>{label}</span>
        <strong>{value}</strong>
        <small>{sub}</small>
      </div>
    </div>
  );
}


function PanelHead({
  eyebrow,
  title,
  right,
}: any) {
  return (
    <div className="panel-head">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h2>{title}</h2>
      </div>

      {right}
    </div>
  );
}


function VehicleRow({
  vehicle: v,
  selected,
  onClick,
}: any) {
  return (
    <button
      className={`vehicle-row ${
        selected ? "chosen" : ""
      }`}
      onClick={onClick}
    >
      <span
        className={`vehicle-symbol ${
          v.status === "BREAKDOWN"
            ? "red"
            : v.status === "DELAYED" ||
              v.status === "STOPPED"
            ? "orange"
            : "green"
        }`}
      >
        <Truck size={15} />
      </span>

      <span className="vehicle-name">
        <b>{v.id}</b>
        <small>
          {v.vehicle_type || "Fleet vehicle"}
          {" · "}
          {v.speed ?? "—"} km/h
        </small>
      </span>

      <Status value={v.status} />
    </button>
  );
}


function Status({ value }: any) {
  const v = String(
    value || "UNKNOWN"
  ).toUpperCase();

  const cls =
    v.includes("BREAK") ||
    v === "FAILED"
      ? "red"
      : v.includes("DELAY") ||
        v === "STOPPED" ||
        v === "WAITING_FOR_HELPER"
      ? "orange"
      : [
          "MOVING",
          "ACTIVE",
          "COMPLETED",
          "FEASIBLE",
          "RECOVERY_ACTIVE",
        ].includes(v)
      ? "green"
      : "blue";

  return (
    <span className={`status ${cls}`}>
      {label(v)}
    </span>
  );
}


function Detail({
  label,
  value,
}: any) {
  return (
    <div className="detail">
      <small>{label}</small>
      <b>{value}</b>
    </div>
  );
}


function Empty({
  text,
}: any) {
  return (
    <div className="empty-state">
      <CircleDot size={19} />
      <span>{text}</span>
    </div>
  );
}


function AlertList({
  alerts,
  onSelect,
}: {
  alerts: FleetAlert[];
  onSelect: (vehicleId: string) => void;
}) {
  return (
    <div className="alert-list">
      {alerts.map((a: FleetAlert, i: number) => (
        <button
          className="alert-row"
          key={a.id || a.alert_id || i}
          onClick={() => onSelect(a.vehicle_id)}
        >
          <span
            className={`alert-icon ${
              a.type === "breakdown_alert"
                ? "red"
                : a.type === "delay_alert"
                ? "orange"
                : "blue"
            }`}
          >
            {a.type === "breakdown_alert" ? (
              <AlertTriangle size={16} />
            ) : a.type === "delay_alert" ? (
              <Clock3 size={16} />
            ) : (
              <MapPin size={16} />
            )}
          </span>

          <span className="alert-copy">
            <b>{a.message || a.type}</b>
            <small>
              {a.vehicle_id} · {fmt(a.timestamp)}
            </small>
          </span>

          <span className="alert-kind">
            {a.type
              ?.replace("_alert", "")
              .replace(/_/g, " ")}
          </span>
        </button>
      ))}

      {!alerts.length && (
        <Empty text="No alerts recorded yet." />
      )}
    </div>
  );
}
