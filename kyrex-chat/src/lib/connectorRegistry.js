// connectorRegistry.js — the Connections hub's generic connector registry and
// card model.
//
// Pure and dependency-free (it borrows only the status derivation from
// ./connections.js), so the hub's STRUCTURE can be verified without React or
// network: node tests/connectorRegistry.test.mjs
//
// Three guarantees are enforced HERE, not merely left to the component:
//
//   * SECRETS NEVER SURFACE — a card is assembled from an allow-list of safe
//     fields, and any secret-shaped key on a live provider view is DROPPED,
//     never copied. The component can therefore only render what is safe.
//   * UNIMPLEMENTED APPS ARE NEVER CONNECTABLE — `connectable` is forced off
//     unless a connector is BOTH implemented and declared connectable, so an
//     app whose integration does not exist yet can never present a live
//     Connect control.
//   * WRITE ACCESS IS SEPARATE — a connector's baseline access is READ-ONLY;
//     a write upgrade is a DISTINCT, explicitly APPROVAL-GATED affordance and
//     is never implied by connecting for read.
import { statusOf } from './connections.js';

export const ACCESS_READ = 'read';
export const ACCESS_READ_WRITE = 'read_write';

export const SECTION_CONNECTED = 'Connected';
export const SECTION_AVAILABLE = 'Available';

export const READ_ONLY_BADGE = 'Read-only';
export const READ_WRITE_BADGE = 'Read & write';
export const COMING_SOON_LABEL = 'Coming soon';

export const SECRET_FREE_NOTICE =
  'Only connection status and granted capabilities are shown. Kyrex never ' +
  'displays or stores a token, client secret, or authorization code here.';

export const WRITE_UPGRADE_NOTICE =
  'Creating calendar events is a separate upgrade. It never happens just ' +
  'because read access is connected — every event is shown to you for ' +
  'explicit approval before it is created.';

// Only these fields may ever be copied off a live provider view onto a card.
export const SAFE_VIEW_FIELDS = Object.freeze([
  'provider', 'status', 'connected', 'expired', 'usable', 'configured',
  'connected_at', 'expires_at', 'read_only', 'has_write_scope',
]);

// Secret-shaped keys are dropped even when a hostile or buggy backend sends
// them: they can never reach a card, and therefore never reach the DOM.
export const SECRET_KEY_RE =
  /(access[_-]?token|refresh[_-]?token|client[_-]?secret|secret|token|authorization|credential|api[_-]?key|private[_-]?key|password|passwd)/i;

// ── The registry ────────────────────────────────────────────────────────
//
// `implemented` is the load-bearing flag: only a connector whose integration
// actually exists may be connected. `google_calendar` is the one real OAuth
// integration; `gmail` is declared as the NEXT, read-only connector and is
// deliberately NON-connectable until it is actually implemented.
export const CONNECTOR_REGISTRY = Object.freeze([
  Object.freeze({
    id: 'google_calendar',
    provider: 'google',
    name: 'Google Calendar',
    category: 'Calendar',
    icon: '📅',
    description:
      'Read your calendars, events, and availability so your Bots can answer ' +
      'questions about your schedule.',
    implemented: true,
    connectable: true,
    access: ACCESS_READ,
    // A SEPARATE, explicitly approval-gated upgrade — never implied by read.
    writeUpgrade: Object.freeze({
      id: 'google_calendar_write',
      label: 'Enable calendar event creation',
      access: ACCESS_READ_WRITE,
      approvalGated: true,
      description:
        "Adds Google's calendar.events write scope. Creating an event still " +
        'requires your explicit approval, every time.',
    }),
  }),
  Object.freeze({
    id: 'gmail',
    provider: 'google',
    name: 'Gmail',
    category: 'Mail',
    icon: '✉️',
    description:
      'Read and search your mail so your Bots can find information. Sending ' +
      'is never granted.',
    implemented: false, //  read-only integration to be added next
    connectable: false, //  unimplemented ⇒ NEVER connectable
    access: ACCESS_READ,
    writeUpgrade: null,
  }),
]);

export function listConnectors() {
  return CONNECTOR_REGISTRY.slice();
}

export function connectorById(id) {
  return CONNECTOR_REGISTRY.find((c) => c.id === id) || null;
}

/**
 * Whether a connector may show a live Connect control.
 *
 * Requires BOTH an existing implementation AND an explicit connectable
 * declaration, so flipping one flag alone can never expose an unimplemented
 * app as connectable.
 */
export function isConnectable(connector) {
  return Boolean(
    connector && connector.implemented === true && connector.connectable === true
  );
}

/** Case-insensitive match over a connector's name, category and description. */
export function connectorMatches(connector, query) {
  const q = String(query == null ? '' : query).trim().toLowerCase();
  if (!q) return true;
  if (!connector) return false;
  const hay = [connector.name, connector.category, connector.description,
    connector.id]
    .filter(Boolean).join(' ').toLowerCase();
  return hay.includes(q);
}

export function searchConnectors(query, registry = CONNECTOR_REGISTRY) {
  return (registry || []).filter((c) => connectorMatches(c, query));
}

/** The live provider view for a connector, or null (only google exists today). */
export function liveViewFor(connector, views) {
  const list = Array.isArray(views) ? views : [];
  if (!connector) return null;
  return list.find((v) => v && v.provider === connector.provider) || null;
}

/**
 * Copy ONLY allow-listed, non-secret fields off a live provider view.
 *
 * A key matching SECRET_KEY_RE is dropped outright; anything not on the
 * allow-list is dropped too. The returned object is safe to hand to a card.
 */
export function safeView(view) {
  const out = {};
  if (!view || typeof view !== 'object') return out;
  for (const key of Object.keys(view)) {
    if (SECRET_KEY_RE.test(key)) continue;
    if (!SAFE_VIEW_FIELDS.includes(key)) continue;
    out[key] = view[key];
  }
  return out;
}

/**
 * The renderable card model for one connector.
 *
 * `view` is the raw live provider view (may be null for a planned connector).
 * No secret-shaped datum is ever copied; `connectable` is the enforced
 * `isConnectable()`; a planned connector reports status "planned".
 */
export function connectorCard(connector, view) {
  const connectable = isConnectable(connector);
  // A planned (unimplemented) connector has NO live status: it never consults
  // a provider view, so two connectors that share a provider (Google Calendar
  // and Gmail are both "google") can never inherit each other's connection.
  const safe = connectable ? safeView(view) : {};
  const status = connectable ? statusOf(safe) : 'planned';
  return {
    id: connector.id,
    provider: connector.provider,
    name: connector.name,
    category: connector.category,
    icon: connector.icon,
    description: connector.description,
    access: connector.access,
    implemented: connector.implemented === true,
    connectable,
    status,
    connected: connectable && (Boolean(safe.connected) || status === 'connected'),
    expired: connectable && status === 'expired',
    configured: Boolean(safe.configured),
    readOnly: safe.read_only !== false,
    hasWriteScope: connectable && Boolean(safe.has_write_scope),
    connectedAt: typeof safe.connected_at === 'number' ? safe.connected_at : null,
    expiresAt: typeof safe.expires_at === 'number' ? safe.expires_at : null,
    writeUpgrade: connector.writeUpgrade || null,
  };
}

/**
 * Build the whole hub model for a query: searched cards split into the
 * Connected and Available sections.
 *
 * A card belongs under Connected while the provider reports connected OR
 * expired (an expired grant still belongs there, with a reconnect hint);
 * everything else — including planned, non-connectable apps — is Available.
 */
export function buildHubModel(views, query = '') {
  const cards = searchConnectors(query).map((connector) =>
    connectorCard(connector, liveViewFor(connector, views))
  );
  const connected = cards.filter((c) => c.connected || c.status === 'expired');
  const available = cards.filter(
    (c) => !(c.connected || c.status === 'expired')
  );
  return {
    query: String(query == null ? '' : query),
    connected,
    available,
    isEmpty: connected.length === 0 && available.length === 0,
  };
}
