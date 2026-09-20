// connections.js — deterministic decisions for the Settings -> Connections UI
// (Google Calendar, read-only). Pure helpers kept out of the component so they
// can be verified without React or network:
//
//   * status derivation — connected | disconnected | expired, from the
//     backend's derived `expired`/`usable` fields (the backend is the source
//     of truth; the UI never re-derives expiry from raw seconds);
//   * a label AND action for each state, including the reconnect hint;
//   * the read-only capability summary with explicit unsupported actions;
//   * redaction — the display guard scrubs anything secret-shaped, so a
//     hostile or malformed response can never render a token or secret.
//
// Run: node --test tests/connections.test.mjs

export const READ_ONLY_NOTICE =
  "Current access is read-only. Kyrex can read your calendar, but it cannot " +
  "create events until you explicitly enable event creation.";

export const WRITE_ENABLED_NOTICE =
  "Event creation is enabled. Every event still requires your explicit approval.";

export const SECRET_NOTICE =
  "No token, client secret, or authorization code is ever shown or stored " +
  "in this interface.";

export const UNAVAILABLE_NOTICE =
  "Connections are unavailable on this host right now. Try again later.";

export const CONNECT_LABEL = "Connect Google Calendar";
export const DISCONNECT_LABEL = "Disconnect";

export function statusOf(connection) {
  if (!connection || typeof connection !== "object") return "unknown";
  if (connection.expired === true) return "expired";
  const s = String(connection.status || "").toLowerCase();
  if (s === "connected") return "connected";
  if (s === "disconnected") return "disconnected";
  return "unknown";
}

export function statusLabelOf(status) {
  switch (status) {
    case "connected": return "Connected";
    case "disconnected": return "Not connected";
    case "expired": return "Expired - reconnect required";
    default: return "Status unknown";
  }
}

export function primaryActionOf(status) {
  switch (status) {
    case "connected": return "disconnect";
    case "disconnected": return "connect";
    case "expired": return "reconnect";
    default: return "refresh";
  }
}

export function needsWriteUpgrade(connection) {
  return statusOf(connection) === "connected" &&
    connection.has_write_scope !== true;
}

export function capabilityLines(connection) {
  const bots =
    (connection && connection.capabilities && connection.capabilities.bots) || {};
  const cal = bots.calendar_bot || {};
  return {
    capabilities: cal.capabilities || [],
    unsupported: cal.unsupported || [],
  };
}

export function safeText(text) {
  if (text === null || text === undefined) return "";
  let out = String(text);
  out = out.replace(
    /\b(access[_-]?token|refresh[_-]?token|client[_-]?secret|api[_-]?key|authorization|credential)["']?\s*[:=]\s*"?[^\s"',}&]+/gi,
    "$1: [redacted]");
  out = out.replace(
    /["'?&](code|state)["']?\s*[:=]\s*"?(?:4\/)?[A-Za-z0-9_.-]{6,}/gi,
    "$1=[redacted]");
  out = out.replace(/https?:\/\/[^/\s:]+:[^/\s@]+@/g, "[redacted]@");
  out = out.replace(/\bya29\.[A-Za-z0-9_.-]{12,}/g, "[redacted]");
  out = out.replace(/\b1\/\/[A-Za-z0-9_.-]{12,}/g, "[redacted]");
  out = out.replace(/\bGOCSPX-[\w-]+/g, "[redacted]");
  return out;
}
