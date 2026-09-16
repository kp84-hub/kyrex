# viewer seccomp profile — provenance and delta

`kyrex-viewer-chromium.json` is the **only** security-profile override the
Tailscale manual viewer uses, and it is bound to the `viewer` service alone in
`browser-host/docker-compose.viewer.yml` (`security_opt` →
`seccomp=./seccomp/kyrex-viewer-chromium.json`). The agent and every other
container stay entirely on Docker's builtin defaults.

## Base allowlist — authoritative provenance

| field | value |
|---|---|
| repository | `moby/profiles` |
| path | `seccomp/default.json` |
| commit | `65adc7e022c97f55e45c054ff012988027733b87` |
| commit date | 2026-08-24 |
| upstream `sha256` | `785b2429264afba4d594320337cb17f144f3c7d51585f9805eef72e28f4f9334` |

This is the profile Docker vendors into the engine line the VPS runs
(Docker Engine **29.1.3**). No public `v29.1.3` tag exists in the upstream
mirror, so the base is pinned by **content hash and upstream commit** rather
than by an engine tag.

Re-derive the provenance at any time:

```sh
curl -sS https://raw.githubusercontent.com/moby/profiles/65adc7e022c97f55e45c054ff012988027733b87/seccomp/default.json \
  | sha256sum
```

The generated profile keeps that document's `defaultAction` (`SCMP_ACT_ERRNO`,
errno `1`), its `archMap`, and every one of its `syscalls` rules unchanged; the
four rules below are **prepended**.

## The delta — four exact amd64 rules

Each is an `SCMP_ACT_ALLOW` with `includes.arches == ["amd64"]` and an **exact
equality** (`SCMP_CMP_EQ`) on `arg0`, one per flag combination observed in the
VPS syscall trace of the successful user-namespace sandbox run:

| # | syscall | `arg0` (decimal) | flags |
|---|---|---|---|
| 1 | `clone` | `268435473` | `CLONE_NEWUSER|SIGCHLD` |
| 2 | `clone` | `1879048209` | `CLONE_NEWUSER|CLONE_NEWPID|CLONE_NEWNET|SIGCHLD` |
| 3 | `clone` | `536870929` | `CLONE_NEWPID|SIGCHLD` |
| 4 | `unshare` | `268435456` | `CLONE_NEWUSER` |

No `mount`, `pivot_root` or `setns` call appeared in the trace, so none is
added.

## What is deliberately NOT widened

* default action stays `SCMP_ACT_ERRNO` — never a global `SCMP_ACT_ALLOW`;
* `mount`, `setns`, `umount`, `umount2`, `bpf`, `perf_event_open` remain
  `CAP_SYS_ADMIN`-gated exactly as Docker ships them (inactive for this
  caps-free container);
* `pivot_root`, `keyctl`, `add_key`, `request_key` are allowed by no rule;
* `ptrace` stays `CAP_SYS_PTRACE`-gated;
* `clone3` keeps Docker's default `SCMP_ACT_ERRNO` with errno **38 (ENOSYS)**
  so glibc falls back to `clone`;
* arbitrary `clone` / arbitrary `unshare` (any other flag combination) stay
  denied.

## Why this is the whole fix

`seccomp=unconfined` with `docker-default` AppArmor still enforcing made
Chromium succeed (rendered `about:blank`, `rc=0`), which proves the blocker was
Docker's builtin **seccomp**, not AppArmor. AppArmor therefore stays at
`docker-default` enforcing and needs **no** customisation. No capability is
added, no container runs in a privileged mode, no host-wide kernel tunable is
changed, and there is no `--no-sandbox` anywhere — the viewer gets a real
multi-process browser sandbox via `--disable-setuid-sandbox`
(`browser-host/viewer_ctl.py`).
