# Images in Kyrex Chat and Kbot

Save a Groq provider connection in Provider settings. An existing Groq key can
be used; voice credentials remain separate and are never copied automatically.
Choose **Bots → Create Vision Bot**, select that connection, and keep the model
`qwen/qwen3.8-27b`. Create the Bot, then choose **Start**. Groq currently lists
this model as preview, so availability can change.

In The Overwatcher or Vision Bot conversation, use the image button, paste an
image, or drop it onto the composer. Up to three PNG, JPEG or still WebP images
are accepted, each at most 5 MB and 16 megapixels. An image-only message asks
for a description. The compact Kbot surface uses the same controls and hosted
backend; this feature does not require another desktop installer.

The Overwatcher submits image analysis to one running, same-owner Vision Bot
through CloudTaskStore and the existing worker/delegation lifecycle. Its report
is passed to Overwatcher's own configured model, with its saved personality and
recent conversation context, to write the final reply. The read result is saved
durably before the reply is generated, under the same task identity. Both model
calls have tools disabled. The visual report stays in
Vision Bot's chat; the original conversation gets Overwatcher's answer, including
after reload/disconnect. If the final reply provider fails, the findings remain
available in Vision Bot's chat and a short failure message is shown instead of
dumping its report into Overwatcher's conversation. A
failed or ambiguous setup returns a clear setup message; there is no repository
fallback and no silent model change. Direct Vision Bot turns use the same
read-only provider adapter. Mention the image/screenshot when asking a follow-up
in Overwatcher; Vision Bot can reuse its recent attached images directly.

The same return path applies to other Chat delegations: Overwatcher uses its own
configured model, saved personality and conversation context for the concise
reply. The specialist keeps the detailed report in its own chat, linked from the
reply. Charts and exact approval/receipt data keep their existing schemas.
Browser research reuses its existing final-answer outbox, with the same persona
and conversation context. A slow or failed reply never changes or retries the
completed action. If a worker stops during presentation, the saved receipt stays
complete and the bounded pending reply expires to a short task-details notice.
Completed specialist findings are also supplied to later Overwatcher engine
turns in that same conversation, including reused sessions. In particular,
image findings are identified as observations from actual uploaded pixels, so
the assistant does not mistake them for a verbal description or ask for the
same image again. Unknown visual details remain unverified.

Image-only analysis advertises no tools, never runs the engine's command or
connected-service tools, and cannot authorize actions. Image text and analysis
remain untrusted evidence. The target's current owner, lifecycle, read policy,
provider profile and model are checked at execution, separately from its name
and prompt. Groq receives normalized image pixels to perform the requested read.

Attachments use random opaque IDs bound to the authenticated owner and exact
conversation. Pixels are encrypted at rest under the deployment secret on the
existing data volume; IDs and private authenticated thumbnail routes, rather
than base64 or public URLs, are stored in transcripts and durable tasks. The
server does not fetch user-supplied URLs. It strips metadata, flattens onto a
white background and resizes images to fit 2048×2048 before sending them. For
small text, upload a tighter crop. Images become unavailable after 30 days;
expired rows are purged on subsequent uploads. Deleting the source conversation
removes its images immediately. Each owner has a 60 MB normalized-image quota.
This deletion controls Kyrex storage, not a provider's retention policy.

Validation uses injected provider responses and offline network guards. It
proves upload, request binding, delegation, adapter payload and recovery behavior;
it does not measure live Groq accuracy. After deploying, upload a known image,
check visible text/details, and try a follow-up before relying on its analysis.
