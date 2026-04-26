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

Workflow runs once per day at **17:00 UTC** = **03:00 AEST / 04:00 AEDT
Melbourne (next day)**, plus `workflow_dispatch` for manual runs from the
GitHub UI. Scheduled this early to absorb GHA cron drift so articles reliably
land before 06:00 Melbourne local. `generate_news.py` still tags articles with
the JST date — at 17:00+ UTC, JST and Melbourne dates always agree, so no
date-skew issues.

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
- **Same-day rerun**: `generate_news.py` short-circuits if `data.json` already
  has entries dated today (JST). Safe to retrigger.
- **Pages build verification step ⚠️**: the final "Verify GitHub Pages build"
  step is informational; a yellow warning there does not fail the run — Pages
  builds occasionally lag past 60s.
