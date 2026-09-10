// ---------------------------------------------------------------------------
// API layer — talks to webapp/backend/api.py (see main.py: app.state.db /
// app.state.exchange_client are wired to the bot's real instances there).
// ---------------------------------------------------------------------------

export const tg = typeof window !== "undefined" ? window.Telegram?.WebApp : null;

export async function apiGet(path) {
  const res = await fetch(`/api${path}`, {
    headers: {
      // Бэкенд валидирует эту строку через HMAC с bot token — см. backend/auth.py
      "X-Telegram-Init-Data": tg?.initData || "",
    },
  });
  if (!res.ok) throw new Error(`API ${path} -> ${res.status}`);
  return res.json();
}

export async function apiPost(path, body) {
  const res = await fetch(`/api${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Init-Data": tg?.initData || "",
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch { /* noop */ }
    throw new Error(detail || `API ${path} -> ${res.status}`);
  }
  return res.json();
}
