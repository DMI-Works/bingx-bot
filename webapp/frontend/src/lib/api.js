// ---------------------------------------------------------------------------
// API layer — talks to webapp/backend/api.py (see main.py: app.state.db /
// app.state.exchange_client are wired to the bot's real instances there).
// ---------------------------------------------------------------------------

export const tg = typeof window !== "undefined" ? window.Telegram?.WebApp : null;

async function request(method, path, body) {
  const headers = {
    // Бэкенд валидирует эту строку через HMAC с bot token — см. backend/auth.py
    "X-Telegram-Init-Data": tg?.initData || "",
  };
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const res = await fetch(`/api${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch { /* noop */ }
    throw new Error(detail || `API ${path} -> ${res.status}`);
  }
  return res.json();
}

export const apiGet = (path) => request("GET", path);
export const apiPost = (path, body) => request("POST", path, body);
export const apiDelete = (path) => request("DELETE", path);
