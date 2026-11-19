export const API_BASE = (
  import.meta.env.VITE_API_BASE_URL ||
  "http://127.0.0.1:8000"
).replace(/\/$/, "");

export const WS_URL =
  import.meta.env.VITE_WS_URL ||
  "ws://127.0.0.1:8000/ws/vehicles";


async function request(
  path: string,
  options: RequestInit = {}
) {
  const response = await fetch(
    `${API_BASE}${path}`,
    {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
    }
  );

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;

    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch {
      // Keep the HTTP status as the fallback error.
    }

    throw new Error(
      typeof detail === "string"
        ? detail
        : JSON.stringify(detail)
    );
  }

  if (response.status === 204) {
    return null;
  }

  return response.json();
}


export const api = {
  get: (path: string, config?: any) => {
    const qs = config?.params
      ? `?${new URLSearchParams(config.params as any)}`
      : "";

    return request(path + qs);
  },

  post: (
    path: string,
    body: any,
    config?: any
  ) => {
    const qs = config?.params
      ? `?${new URLSearchParams(config.params as any)}`
      : "";

    return request(
      path + qs,
      {
        method: "POST",
        body: body == null
          ? undefined
          : JSON.stringify(body),
      }
    );
  },
};


export const getVehicles = () =>
  api.get("/vehicles/");


export const getVehicleCargo = (id: string) =>
  api.get(
    `/vehicles/${encodeURIComponent(id)}/cargo`
  );


export const getGeofences = () =>
  api.get(
    "/geofences/",
    {
      params: {
        active_only: true,
      },
    }
  );


export const getAlerts = () =>
  api.get(
    "/alerts/",
    {
      params: {
        limit: 100,
      },
    }
  ).then((r: any) => r.alerts || []);


export const getHealth = () =>
  api.get("/health");


export const getHistory = (id: string) =>
  api.get(
    `/vehicles/${encodeURIComponent(id)}/history`,
    {
      params: {
        limit: 30,
      },
    }
  );


export const analyzeRecovery = (body: any) =>
  api.post("/recovery/analyze", body);


export const executeRecovery = (
  body: any,
  analysisId: string,
  delayedId: string
) =>
  api.post(
    "/recovery/execute",
    body,
    {
      params: {
        analysis_id: analysisId,
        delayed_vehicle_id: delayedId,
      },
    }
  );


export const getRecoveryPlans = () =>
  api.get("/recovery/plans");


export const getRecoveryHistory = (id: string) =>
  api.get(
    `/recovery/plans/${encodeURIComponent(id)}/history`
  );


export function errorMessage(e: any) {
  return e?.message || "Unexpected error";
}