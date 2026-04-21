# Hermes Agent — Zona fork

This is a personal fork of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent),
maintained at https://github.com/synikitin/hermes-agent. The Zona application
(https://github.com/synikitin/zona) consumes the base Docker image built from
this fork while we wait for the patches below to land upstream.

## Branches

| Branch | Purpose |
|---|---|
| `main` | Pristine mirror of `upstream/main`. Never edited. |
| `zona/main` | Production deploy branch. Rebased forward on upstream tags (currently `v2026.4.16`). The fork's CI builds from here and pushes `:zona-stable` to Zona's ECR. |
| `upstream-tracker` | Tracks `upstream/main` for cleanly-rebased upstream PRs. |
| `upstream-pr/streaming` | Future upstream PR branch for SSE streaming guardrails. |
| `upstream-pr/doubling` | Branch with the conversation-history doubling fix, opened upstream as a PR. |

Tag pattern: `zona-<upstream-tag>+<n>` (e.g. `zona-v2026.4.16+1`).

## Patches on `zona/main`

1. **Conversation-history doubling fix** — `gateway/platforms/api_server.py`
   stored `conversation_history + result["messages"]`, but `result["messages"]`
   already starts with a copy of `conversation_history` (see
   `run_agent.py: messages = list(conversation_history)`). Concatenating prior
   history into itself doubled stored history every turn, producing
   exponential growth instead of the linear `2*N + 1` shape the agent loop
   actually emits. Fix: write `list(result["messages"])` directly to
   `_response_store`. Applied on both the streaming (`_write_sse_responses`)
   and non-streaming (`_handle_responses`) paths.

2. **Production observability** — new `gateway/platforms/metrics.py` exposes
   Prometheus counters/histograms/gauges for the Responses API streaming
   path; `_handle_metrics` serves them at `/metrics`. The
   `hermes_responses_history_length` histogram is the runtime canary for
   the doubling regression — if it ever climbs into the 1000+ buckets,
   we've regressed and the unit tests in
   `tests/gateway/test_responses_chaining.py` would have already caught it.

3. **Structured stream lifecycle logs** — `_write_sse_responses` now emits
   `responses.stream_open`, `responses.first_token`, `responses.tool_call`,
   `responses.stream_complete`, and `responses.stream_aborted` log events
   with the caller's `X-Request-Id` propagated, so a single ID greps both
   Hermes and Zona logs.

## Rebase procedure (run weekly or after upstream tag)

```bash
cd ~/Projects/hermes-agent
git switch upstream-tracker
git pull --ff-only upstream main
git switch zona/main
git rebase upstream-tracker
# resolve any conflicts in the patches above (the doubling fix is the
# most fragile — it sits inside _handle_responses and _write_sse_responses
# which churn upstream).
python -m pytest tests/gateway/test_responses_streaming.py \
                 tests/gateway/test_responses_chaining.py \
                 tests/gateway/test_api_server.py
git push --force-with-lease origin zona/main
```

After the push, GitHub Actions runs `.github/workflows/build-zona.yml`,
which gates on the gateway tests above and pushes `:zona-stable` +
`:<git-sha>` to Zona's `zona-hermes-base` ECR repo.

## Required GitHub Actions secrets (`synikitin/hermes-agent`)

| Secret | Value |
|---|---|
| `AWS_ROLE_TO_ASSUME` | ARN of `zona-hermes-fork-ecr-push` (output `hermes_fork_ecr_push_role_arn` from Zona's Terraform) |
| `AWS_REGION` | e.g. `us-east-1` |
| `ECR_REPOSITORY` | `zona-hermes-base` |

The trust policy on the IAM role pins both repo and branch via the OIDC
`sub` claim: `repo:synikitin/hermes-agent:ref:refs/heads/zona/main`.
