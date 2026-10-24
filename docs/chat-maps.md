# Current location and Google Maps

In an Overwatcher chat, ask a distance or directions question. A contextual
**Use current location** control appears above the composer; it uses the
browser/OS permission prompt for a single position fix. After permission has
been granted, later location-related text and browser voice turns can refresh
the fix automatically. Ordinary messages never request location. There is no
background watcher, location history, remembered home address or IP-location
fallback. Remove an attached fix with its × control, or revoke permission in
the browser to stop future fixes. Desktop accuracy depends on the OS; the Maps
link still works if the webview cannot provide location.

The authenticated Chat request carries a validated location to the host. Raw
coordinates are not put in transcripts, engine prompts, tool arguments/results,
or saved memory, and are cleared from the reused session after the turn. Fixes
older than five minutes and accuracy worse than one kilometer cannot supply
a route origin. The owner can instead explicitly type a starting address.
This is not a background or automation location capability.

Straightforward distance/directions requests run through this adapter directly
in the current authenticated Chat turn. They do not depend on a model choosing
the Maps tool or producing a relevant reply. The answer contains only the
adapter's verified route estimate, an honest unavailable state or a location
clarification, with a Google Maps link. More complex requests retain the engine
tool path. A missing origin also shows **Use current location** in the reply,
including after reload. The button submits only that route with a fresh fix;
switching conversations discards a late permission result. A typed starting
address can continue the latest route clarification in the same conversation.

The host-mediated `maps_route` tool uses the existing Python engine and
confirmation transport. The current owner request grants a public route read;
the live Bot owner/lifecycle and explicit `maps:read` restrictions (including
wildcards and elevated tiers) still apply. Specialized fitness-only surfaces
cannot acquire Maps access. No extra queue, worker or agent loop is added.
This tool serves Overwatcher and other eligible engine-backed Chat sessions;
ordinary pure-model Chat has no new tool loop.

The returned **Open in Google Maps** link works without a Maps API key. On
supported phones it opens the Maps app; otherwise it opens the website. It
omits the origin for device-based routes so Maps can use the device’s location
at the time the link is opened. An explicitly typed starting address is retained.
Opening the link is the user’s action; Kyrex does not start navigation itself.

For distance and travel-time estimates inside Kyrex, configure the backend
secret `GOOGLE_MAPS_API_KEY` for a Google Cloud project with billing and the
**Routes API** enabled. Keep the key server-side, restrict it to the Routes API,
and use appropriate quotas. A Google Calendar OAuth connection does not supply
this key. The adapter sends the consented coordinates or typed origin and
destination only to the fixed Google Routes endpoint, with tools/LLM credentials
kept separate. Driving estimates request traffic-aware routing; other supported
modes are walking, cycling and transit. Costs follow your Maps Platform plan.

Missing configuration, denied/stale/coarse location, invalid or partial geocodes,
no route and provider failures never produce invented miles or minutes. Kyrex
offers a clarification or the Maps link instead. Specify an exact destination
or address if several stores could match; a generic town-distance search is
not a replacement for a verified route.

References: [Maps URLs](https://developers.google.com/maps/documentation/urls/get-started),
[Compute Routes](https://developers.google.com/maps/documentation/routes/compute_route_directions),
[browser location](https://developer.mozilla.org/en-US/docs/Web/API/Geolocation/getCurrentPosition).
