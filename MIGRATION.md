# GitHub Actions Migration Notes

This repo's daily article generation + deployment is now driven by GitHub
Actions instead of the local Mac launchd job + Cowork scheduled tasks.

## What runs where

| Step | Old (local) | New (GHA) |
| --- | --- | --- |
| Generate today's 3 articles | Cowork scheduled task `daily-urban-design-news` (Claude prompt) | `scripts/generate_news.py` (Anthropic API + web_search) |
| Replace Unsplash placeholders with real og:image | `~/bin/urban-design-daily-push.sh` (inline Python) | `scripts/enrich_thumbnails.py` |
| Re-inline `data.json` into `index.html` | `~/bin/urban-design-daily-push.sh` (inline Python) | `scripts/inline_data.py` |
| Commit & push | Cowork scheduled task `deploy-urban-design-site` | last steps of `.github/workflows/daily.yml` |

## Cron

Workflow runs once per day at **14:00 UTC** = **00:00 AEST (winter) /
01:00 AEDT (summer) Melbourne (next day)**, plus `workflow_dispatch` for
manual runs from the GitHub UI. Scheduled this early because empirical GHA
cron drift on this repo has been 2–3.5 hours; with 4h of slack the run
reliably lands before 06:00 Melbourne local. `generate_news.py` uses the
`Australia/Melbourne` timezone for `TODAY`, so the digest is dated by the
reader's local calendar (AEST/AEDT auto-detected).

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
