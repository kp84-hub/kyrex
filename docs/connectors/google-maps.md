# Google Maps driving routes

Kyrex's `maps_route` Chat tool performs one server-side Google Routes API lookup
for a driving distance, traffic-aware duration and estimated arrival. It works
from any running Bot belonging to an explicitly enabled owner, including The
Overwatcher. Jev sees `maps_route` in the owner's available shared tools; Jev
chooses a Bot, and Kyrex remains authoritative for the tool call and permissions.

## Server setup

1. Create a Google Maps Platform project with billing and the Routes API enabled.
2. Create a dedicated API key and restrict its API access to Routes API. Use a
   compatible server-side application restriction for the deployment's egress.
3. Add these variables to the **Kyrex Cloud Railway service**, not the browser,
   Bot prompt, frontend build, Browser Host or public repository:

   ```text
   GOOGLE_MAPS_API_KEY=<your private key>
   KYREX_MAPS_ROUTE_OWNERS=kp84-hub
   ```

   The owners are exact authenticated Kyrex account names, comma separated for
   multiple owners. An empty owner list denies every owner. Use your actual
   Kyrex login if it differs from the example. Deploy/restart Cloud after setup.
4. Open a Bot-bound conversation and ask:

   > How long is the drive from Willow Spring, NC to Coquette Brasserie, 4351 The Circle at North Hills St, Raleigh, NC, leaving now?

   The address in this example is input to verify, not a hardcoded destination.
   If you need a door-to-door estimate, provide your precise origin privately in
   Chat. A town-level origin is explicitly identified as an area-level estimate.

A successful answer must attribute the estimate to Google Maps, preserve any
area-level caveat, and may link the verified endpoint place IDs in Google Maps.
That link opens the endpoints; Maps can recompute a different route/traffic
estimate when opened. Do not describe it as a frozen copy of the API route.

## Boundaries

The read-only operation is `maps:route`, tier 0. It is an owner-enabled shared
service, separate from a Bot's persona or legacy permissions preset. An explicit
`maps:route` or `maps:*` denial/raised tier blocks it. The host checks the live
owner, running Bot, enabled owner list and key again for every call, including
reused sessions. Unbound conversation-only Chat and local TUI/IDE sessions do not
receive this host tool. The key is removed from the engine's environment.

The API endpoint, driving mode, traffic preference, response fields and timeouts
are host-owned. There are at most three lookups per Chat turn, one recommended
route per lookup, no redirects and no automatic retries. Each lookup can incur
Google Maps Platform charges; monitor the project's quotas and billing.

The response uses Google's traffic-aware `duration`, not `staticDuration`.
Future departures require an ISO date/time with a timezone offset. Missing
addresses, partial geocoding, traffic fallback, unavailable routes, invalid
responses, access/billing errors and provider outages are explicit failures;
Kyrex must not substitute web-search guesses. An estimate excludes parking and
walking and may change with traffic.

This version does not use live device location, Places autocomplete, public
transit or an arrival-time constraint for driving. Calendar + Maps leave-time
planning requires a verified event location and a departure-based estimate;
Google's driving `arrivalTime` field is not supported. It creates no calendar
events or messages. The connector creates no separate location-history store;
ordinary Chat/engine transcripts retain their usual conversation evidence.

## References and validation

- [Compute Routes REST reference](https://developers.google.com/maps/documentation/routes/reference/rest/v2/TopLevel/computeRoutes)
- [Waypoint address inputs](https://developers.google.com/maps/documentation/routes/reference/rest/v2/Waypoint)
- [Google Maps URLs](https://developers.google.com/maps/documentation/urls/guide)

Tests use provider fixtures and the actual engine-to-host request/reply boundary.
Live Google routing needs the server configuration above; no API key is bundled
or requested through a model conversation.
