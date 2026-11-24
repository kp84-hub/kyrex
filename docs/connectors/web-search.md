# API web search in Kyrex Chat

Open **Connections → Web search**, get a Brave Search API key from
[Brave's dashboard](https://api-dashboard.search.brave.com/), and save it in
the password field. Queries are sent to Brave, and its usage charges follow
the owner's provider plan. Saving a key makes no provider request and does
not claim the key has been verified. API access is checked on the first search.

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
Views contain only provider/configured/update metadata, never the key. The
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

Requests use Brave's fixed HTTPS web-search endpoint and subscription-token
header, with redirects disabled, a ten-second network timeout, a 1 MiB
response bound, at most ten results and three calls per Chat turn. SQLite
enforces one request per second per owner across conversations/processes.
There are no automatic retries, search-query database records or added result
caches. Provider billing limits remain in the owner's Brave account.

Deploy the Cloud backend/worker and Chat frontend containing this change,
then save a key in Connections. Browser Host and local TUI updates are not
needed for this Chat integration. A deployed key and successful real provider
request are still required for live verification; mocked API tests and UI
builds do not establish that outcome.

Provider references:

- [Web Search API](https://api-dashboard.search.brave.com/api-reference/web/search/get)
- [Key authentication](https://api-dashboard.search.brave.com/documentation/guides/authentication)
