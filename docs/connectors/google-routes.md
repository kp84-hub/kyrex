# Google Maps Routes in Kyrex Chat

Bot-bound Chat and workspace-attached read-only Chat expose `maps_route(origin,
destination)`. The Overwatcher uses it directly for driving questions rather
than delegating to Browser Bot. Ordinary Chat without a Bot or workspace keeps
its existing conversation-only behavior.

## Server setup

1. In Google Cloud, enable **Routes API** and billing for your project.
2. Create a server API key restricted to **Routes API**. Configure applicable
   server restrictions and quota limits in the Google Cloud project.
3. Add `KYREX_GOOGLE_MAPS_API_KEY` to the **Railway web service** and redeploy.
   Do not put this key in Chat, source control, the browser frontend or an OVH
   Browser Host. Calendar/Gmail OAuth does not enable this API.

The key stays in the backend and is removed from the Chat engine subprocess
environment. No browser login, pairing, Google consent page or OVH rebuild is
needed. Google bills Routes requests; traffic-aware routing uses its applicable
SKU. See [setup](https://developers.google.com/maps/documentation/routes/get-api-key)
and [usage and billing](https://developers.google.com/maps/documentation/routes/usage-and-billing).

## Behavior

Ask The Overwatcher: “How long does it take to drive from Willow Spring, NC to
Crabtree Valley Mall in Raleigh? Give me the Maps link.”

The tool makes one bounded POST to Google's `computeRoutes` endpoint with
`DRIVE` and `TRAFFIC_AWARE`. It returns Google's travel-time estimate for leaving
now, distance, source, fetch time and a key-free Google Maps directions URL.
The directions link can recalculate a different route when opened. There are
no future departure forecasts, turn-by-turn instructions or live GPS inputs in
this version. A town-level origin is approximate; a specific starting address
gives a more useful estimate. Missing locations should prompt a question.

Missing configuration, access/billing/key problems, quota exhaustion, timeout,
no route and malformed data have distinct safe errors. Failures may still return
a directions link but never a fabricated verified ETA. Requests do not retry
automatically. Stop remains responsive. Provider response bodies and exception
text are not sent to the model. No route snapshots or addresses are stored in
a new database; ordinary conversation history keeps its existing behavior.

Offline tests cover Google request shape, units, bounded responses, key
isolation, failures, tool dispatch and Chat routing. A real Google response must
be checked after the server key is configured.
