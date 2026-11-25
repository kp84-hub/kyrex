# API web search in Kyrex Chat

Open **Connections → Web search**, get a Tavily API key from
[Tavily's dashboard](https://app.tavily.com/), and save it in the password field.
Choose the free Researcher plan and keep pay-as-you-go disabled in Tavily.
It includes 1,000 monthly credits without a credit card; basic search uses one
credit per request. When the allowance is exhausted, search reports the limit
instead of retrying or enabling paid usage. Kyrex cannot change or guarantee
the billing settings of a provider account; a paid/PAYG-enabled key follows
that account's billing settings.

Queries are sent to Tavily. Saving a key makes no provider request and does
not claim the key has been verified. API access is checked on the first search.
Previously saved Brave keys are marked as requiring reconnection and never
sent to Tavily. Replace the key or disconnect it in Connections. Keys must
start with `tvly-`; credentials are bound to their provider in encrypted storage.

The authenticated Chat engine, including The Overwatcher, receives a
`web_search` tool when the owner has saved a key and the current Bot permits
the operation. Results contain bounded titles, links and snippets from the
search index. They are explicitly untrusted and do not establish live prices,
stock, opening hours, source-page content or successful browser access. Use
Browser Bot to read the returned website before making current claims, with
the Browser's existing owner, domain, host and read-only boundaries intact.
Direct Browser Bot tasks retain their existing page-reading execution path;
the API discovery tool belongs to the Chat engine, not the Browser Host.

When API search is available, coordinator delegation refuses query navigations
to the known Google, DuckDuckGo, Bing and Brave search-page endpoints. It asks
the engine to use API discovery and then delegate the actual website link.
Ordinary website searches, Google Maps and source-page reads are preserved.
This is not a CAPTCHA bypass or a grant to browse new domains. Missing or
rejected keys, provider outages and rate limits have distinct errors; a valid
empty response is a successful search with no matching eligible links. The
guidance does not fall back to cycling through search-engine websites.

Keys are owner-scoped and sealed using the existing connector encryption.
Views contain only provider/configured/reconnect/update metadata, never the key. The
settings API requires authentication and a same-origin mutation, suppresses
credential-bearing validation errors, and marks responses `no-store`.
There is no global environment-key fallback and no provider-key transfer to
the engine, Browser Host, prompts or frontend storage. Only the typed query
and result count reach the host through the existing engine confirmation
protocol. The engine cannot select an owner, credential, endpoint or header.

An owner-connected key grants only public `web:search`. Live explicit Bot
denies, raised tiers, malformed policies, stopped Bots and owner mismatches
block it. Restricted fitness surfaces do not receive the tool. Grants are
refreshed each turn and checked again at the provider boundary and before
returning results. Disconnecting or replacing a key invalidates in-flight
results. Discovery never changes repository read-only mode or grants a
delegated effect. Unbound terminal/headless engines do not gain this tool.

Requests POST JSON to Tavily's fixed HTTPS search endpoint with a Bearer
header. The host fixes `search_depth=basic`, `auto_parameters=false` and
`topic=general`, with answers, raw content and images disabled. Bots cannot
select advanced search or automatic parameter upgrades. Redirects are disabled,
with a ten-second network timeout, a 1 MiB response bound, at most ten results
and three calls per Chat turn. SQLite enforces one request per second per owner
across conversations/processes. There are no automatic retries, query records
in this connection store or added result caches. Provider billing limits remain
in the owner's Tavily account. HTTP 429/432/433 are reported as rate/quota limits;
invalid credentials and temporary failures remain distinct from empty results.

Deploy the Cloud backend/worker and Chat frontend containing this change,
then save a key in Connections. Browser Host and local TUI updates are not
needed for this Chat integration. A deployed key and successful real provider
request are still required for live verification; mocked API tests and UI
builds do not establish that outcome.

Provider references:

- [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)
- [Billing, credits and usage limits](https://help.tavily.com/articles/8298987937-billing-credits-usage-limits)
