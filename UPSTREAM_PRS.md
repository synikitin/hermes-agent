# Upstream PR tracker

Patches carried on `zona/main` that we plan to (or have) propose(d) upstream
to [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).

| Patch | Branch (this fork) | Status | Upstream PR |
|---|---|---|---|
| Conversation-history doubling fix on `/v1/responses` | `upstream-pr/doubling` | TODO | — |
| Streaming guardrails (logs, metrics, /metrics endpoint) | `upstream-pr/streaming` | DRAFT (rebase + de-Zona) | — |

## Workflow for each upstream PR

1. `git switch upstream-tracker && git pull --ff-only upstream main`
2. `git switch -c upstream-pr/<topic> upstream-tracker`
3. Cherry-pick the relevant `zona/main` commits, **stripping any
   "ZONA-FORK" comments and references to Zona/synikitin** from commit
   messages and code so the PR reads as a generic upstream contribution.
4. Run `python -m pytest tests/gateway/` to confirm green.
5. `git push origin upstream-pr/<topic>`
6. Open a PR at NousResearch/hermes-agent from this branch.
7. Update the table above with the PR URL and status.
