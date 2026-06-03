# Upstream provenance — vendors/hermes-agent

| Field | Value |
|---|---|
| Source repo | `https://github.com/NousResearch/hermes-agent` |
| Vendored commit | `b9a9551baf95691d64a4ce92d68d6366475a3fd5` (tag `v2026.5.29`, version **0.15.1**) |
| Upstream PR at vendor time | release `v0.15.1 (2026.5.29)` (#34222) |
| Vendor date | 2026-06-02 (bumped from `486b692` / v0.13.0, 2026-05-12; kadabra #415) |
| Vendor method | `git clone --filter=blob:none` → checkout tag → `rsync -a --delete` (excludes below); operator patches re-applied from `.local-patches/` via 3-way merge. |
| Reason to vendor | Patch `gateway/platforms/slack.py` for hermes-work public-channel UX (strict @-mention, ephemeral intermediate, draft-confirm final) + Option A `default_channel_policy` + cron `is_cron` bypass. See `docs/hermes-work-public-channel-spec.md`. |
| Tracking project | kadabra `hermes-work-public-channel-ux` (id 27); bump tracked in #415. |

## Local patches (re-apply after every re-pull — `.local-patches/`)

| # | File(s) | Concern | Refs |
|---|---|---|---|
| 01 | `gateway/channel_directory.py` | drop `private_channel` from `users.conversations` types (bot lacks `groups:read`) | log-flood fix |
| 02 | `gateway/platforms/slack.py` | public-channel UX + Option A `default_channel_policy` + `is_cron` bypass | #169/#421/#435 |
| 03 | `gateway/platforms/base.py` | `is_final` metadata flag (set here, read by slack gating) | #421/#435 |
| 04 | `cron/scheduler.py` | `is_cron`/`is_final` on cron `send_metadata` | #435 |
| 05 | `tests/cron/test_scheduler.py` | is_cron test + upstream thread-fallback metadata expectation | #435 |
| 06 | `tests/gateway/test_slack_mention.py` | strict @-mention test | #169 |
| — | `tests/gateway/test_slack_ephemeral_and_approval.py` | operator-NEW guard test (whole file; survives via no-upstream-counterpart) | #421/#435 |

All 02–06 verified `git apply --check`-clean against pure upstream 0.15.1 on 2026-06-02.

## Dropped on this bump — GEMINI (operator decision, kadabra #415, 2026-06-02)

Our local Gemini adapter (`agent/google_adapter.py`, `agent/transports/google.py`,
+ `hermes_cli/providers.py` `google` overlay + `agent/transports/__init__.py`
discovery + `run_agent.py` `google_messages` branch) was **removed** — these files
are now pure upstream. Upstream 0.15.1's native google is `gemini-cli`/CloudCode
**OAuth** (`cloudcode-pa://google`), which is NOT the direct-API-key path our
deploy used.

**⚠ DEPLOY-TIME ACTION (operator):** the live devvm `config.yaml` `fallback_model`
still references `provider: google` / `gemini-2.5-flash` /
`base_url: https://generativelanguage.googleapis.com/v1beta` — that path no longer
exists after this bump. **Remove the gemini `fallback_model` entry** (leaving
`openrouter` llama as fallback) when deploying this vendored tree via
`scripts/deploy_hermes_code.sh`, or the fallback chain will fail to initialize.

## Re-pull procedure

```sh
TMP=$(mktemp -d)
git clone --depth=1 https://github.com/NousResearch/hermes-agent "$TMP/hermes-agent"
NEW_HEAD=$(git -C "$TMP/hermes-agent" rev-parse HEAD)
rsync -a --delete \
  --exclude=__pycache__ --exclude='*.pyc' --exclude=.git \
  --exclude=.github --exclude=website \
  --exclude=UPSTREAM.md --exclude='*.local-patch.diff' \
  "$TMP/hermes-agent/" vendors/hermes-agent/
# Re-apply local patches in vendors/hermes-agent/*.local-patch.diff if any.
# Update the table above with the new commit hash.
```

## Local-patch policy

Patches we own live in `gateway/platforms/slack.py` and adjacent test files. To make
re-pulls auditable, prefer either:
- A small overlay (e.g. a subclass / monkey-patch in a separate `*_overlays.py` file
  outside the vendored tree), OR
- A clearly-marked local edit fenced with `# --- domi-local-patch ---` comments at
  the top and bottom of each modified hunk.

## Out-of-scope on vendor

- `__pycache__/` and `*.pyc`: excluded (rsync filter).
- `.git/`: excluded (would otherwise be tracked as a submodule).
- `.github/`: excluded — upstream's 10 workflows (lint, docker-publish,
  deploy-site, osv-scanner, etc.) would otherwise run on this repo's CI and
  collide with our own.
- `website/`: excluded — 15M docs site (Docusaurus build + screenshots) not
  load-bearing for the patches we own.
- The upstream `RELEASE_v*.md` are kept (they're small and document upstream
  feature history relevant to choosing config flags).

When re-pulling, remove these directories AGAIN after the rsync.
