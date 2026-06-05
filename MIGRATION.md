# Pipeline Migration Notes

> **Current state (since 2026-06-05):** daily curation runs as a Claude Code
> scheduled task that uses the user's Claude subscription (no Anthropic API
> spend). The GitHub Actions workflow is retained as a manual fallback only.
> Details in [Generation runtime](#generation-runtime) below.

This repo's daily article generation + deployment has gone through three
runtimes:

1. **Mac launchd + Cowork** (original) — local automation on the user's Mac.
2. **GitHub Actions** (interim) — moved to GHA cron + Anthropic API.
3. **Claude Code scheduled task** (current) — runs while the user has Claude
   Code open, no API key needed.

## What runs where

| Step | Old (local) | New (GHA) |
| --- | --- | --- |
| Generate today's 3 articles | Cowork scheduled task `daily-urban-design-news` (Claude prompt) | `scripts/generate_news.py` (Anthropic API + web_search) |
| Replace Unsplash placeholders with real og:image | `~/bin/urban-design-daily-push.sh` (inline Python) | `scripts/enrich_thumbnails.py` |
| Re-inline `data.json` into `index.html` | `~/bin/urban-design-daily-push.sh` (inline Python) | `scripts/inline_data.py` |
| Commit & push | Cowork scheduled task `deploy-urban-design-site` | last steps of `.github/workflows/daily.yml` |

## Generation runtime

**Primary (since 2026-06-05): Claude Code scheduled task.**
A task called `urban-design-daily-update` lives at
`~/.claude/scheduled-tasks/urban-design-daily-update/SKILL.md` and fires at
**06:00 Melbourne local time** every day. The task uses Claude Code's
`WebSearch` tool to find articles, edits `data.json` directly, runs
`scripts/enrich_thumbnails.py` + `scripts/inline_data.py`, and pushes the
result to GitHub. Because the LLM work happens inside the Claude Code
session, it is billed against the user's Claude subscription (Pro/Max),
NOT against `ANTHROPIC_API_KEY` — monthly Anthropic API spend drops to $0.

The task runs only when Claude Code is open. If the app is closed at 06:00,
the run is deferred until next launch. Practical implication: open Claude
Code at least once per day to keep the site fresh; expect occasional same-
day "delivered at the morning's first launch" instead of "delivered at
06:00 sharp".

**Fallback (manual): GitHub Actions workflow.**
The `.github/workflows/daily.yml` workflow is kept but its cron schedule is
commented out. It can still be triggered manually for backfills via
`gh workflow run "Daily urban design digest"` — that path still uses
`ANTHROPIC_API_KEY` and incurs Anthropic API cost (~$0.30 per run), so use
it sparingly.

### Why we migrated off the GHA cron

- **Cost:** May 2026 ran $16–21 on Anthropic API alone for ~30 runs. The
  Claude Code task uses the existing subscription instead.
- **Cron drift:** GHA scheduled events drift 2–5 hours during peak UTC,
  which made the 06:00 Melbourne SLA unreliable on bad days.
- **Rate limits:** Tier-1 30k tokens/min ceiling caused frequent 429-driven
  partial-day runs that needed manual rerun anyway.

The Claude Code task isn't free of caveats — it depends on the user opening
Claude Code daily — but that constraint is easy to satisfy and is
fundamentally cheaper than dedicated API calls.

## Required setup (one-time)

1. **Add the API key as a repo secret.**
   GitHub → repo → Settings → Secrets and variables → Actions → New
   repository secret:
   - Name: `ANTHROPIC_API_KEY`
   - Value: your Anthropic Console key
2. **(Optional) Override the model.**
   Under the same page, "Variables" tab → New repository variable:
   - Name: `ANTHROPIC_MODEL`
   - Value: e.g. `claude-sonnet-4-5` or `claude-opus-4-6`
   Defaults to `claude-sonnet-4-5` if not set.
3. **Workflow permissions.**
   Settings → Actions → General → Workflow permissions → make sure "Read and
   write permissions" is enabled (so the Action can push back to `main`).
4. **First manual run.**
   Actions tab → "Daily urban design digest" → Run workflow → branch `main`.
   Confirm green, check the live site.
5. **Disable the local automations.**
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.shapovalov.urban-design-daily-push.plist
   ```
   And in Cowork: disable the scheduled tasks `daily-urban-design-news` and
   `deploy-urban-design-site`.

## Token note

`GITHUB_TOKEN` (used by the workflow's `git push`) is auto-provided by GitHub
Actions on every run — no manual setup. `ANTHROPIC_API_KEY` is the only secret
you have to register.

## Local rotation

The local repo's `.git/config` has a Personal Access Token embedded in the
`origin` URL. Once GHA is the source of truth, you can — and should — rotate
that token. Ad hoc local pushes after rotation will use SSH or `gh auth login`.

## Failure modes

- **Anthropic API error**: workflow fails; rerun manually or wait for next
  day's cron. No partial commits because the script writes data.json only
  after a successful API call.
- **Hard target of 3 articles/day, ≥1 Australian**: `generate_news.py`
  retries the model up to 4 times, asking for "the missing N" each pass and
  feeding back the cumulative dedup list. The prompt also enforces a
  regional constraint: at least 1 of the 3 must be Australia-domestic
  (federal/state policy, an Australian city's planning news, etc.). The
  script tracks Australian count using the article's `topics` tags
  (case-insensitive match against a fixed Australia/state/city list). On
  each retry it tells the model how many AU articles are still needed.
  There is a 70-second sleep between attempts to let the Anthropic
  30k-tokens/min rate-limit window roll over. The script always exits 0
  when it produced ≥1 article — partial days *are* committed so the live
  site has content. A final workflow step "Verify daily target" fails the
  run red when `data.json` ends with fewer than 3 articles or zero
  Australian articles for the Melbourne date, so any gap stays visible.
- **Same-day rerun**: `generate_news.py` short-circuits only when
  `data.json` already has the full daily target (3) for the Melbourne date.
  After a partial day, a rerun reads the existing N entries and asks the
  model for the missing 3-N — a clean backfill.
- **GHA cron drift caveat**: empirical drift on this repo's scheduled runs
  has been 2–5 hours during peak UTC. The 14:00 UTC schedule with 4h of
  slack works *most* days; on a 5h-drift day we hit the AEDT 06:00 deadline
  by minutes. If a missed-SLA day is unacceptable, options are: (a) add a
  paid GHA tier or self-hosted runner, (b) replace GHA cron with an external
  trigger (Cloudflare Cron Triggers / AWS EventBridge → `workflow_dispatch`
  via REST API), or (c) live with occasional ~07:00 deliveries.
- **Pages build verification step ⚠️**: the final "Verify GitHub Pages build"
  step is informational; a yellow warning there does not fail the run — Pages
  builds occasionally lag past 60s.
