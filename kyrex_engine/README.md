# Kyrex Engine

## Jev shadow observation

Kyrex can pass metadata-only descriptions of allowed, proposed tool calls to
TypeSafe Jev for passive evaluation. Shadow results are telemetry: they never
approve, deny, modify, delay, or execute a tool call.

Enable it explicitly with both variables:

```bash
export TYPESAFE_API_KEY="..."
export KYREX_JEV_SHADOW=1
```

Cloud conversations write `jev_shadow.jsonl` inside their isolated session
directory. Other surfaces default to `~/.kyrex/jev_shadow.jsonl`; override that
location with `KYREX_JEV_SHADOW_LOG`.
