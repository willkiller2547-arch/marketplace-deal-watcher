# Marketplace Deal Watcher v3

Windows GUI watcher for Facebook Marketplace searches with Discord alerts and optional AI deal analysis.

## What v3 adds

- Hybrid deal scoring: cheap local rules shortlist listings first, then AI analyzes only promising candidates.
- Optional listing-photo analysis.
- Structured AI output: deal score, confidence, verdict, estimated value range, positives, and red flags.
- AI results are included in Discord alerts.
- The OpenAI API key is stored in Windows Credential Manager through `keyring`; it is not stored in `config.json` or committed to GitHub.
- AI analysis is cached in SQLite so unchanged listings are not repeatedly sent to the API.
- Price changes can trigger a fresh AI analysis.
- The first scan can learn current listings without spending API credits when **Alert on first scan** is disabled.

## Quick start

1. Clone/download the repository.
2. Double-click `SETUP.bat`.
3. Double-click `START.bat`.
4. Paste your Discord webhook and click **Test Discord**.
5. Click **Facebook Login**, sign in normally, and close the browser after Marketplace is visible.
6. Build a Marketplace search in Facebook, copy its results URL, and add it with **Add Search**.
7. Click **Run Once** to test normal detection.

## Enable AI analysis

If you have access to an OpenAI API project you are permitted to use:

1. In the **AI Deal Analysis** section, paste the project's API key.
2. Click **Save Key**. The key is stored in Windows Credential Manager.
3. Leave the default model as `gpt-5-mini` unless you specifically want another compatible model.
4. Click **Test AI**.
5. Turn on **Enable AI analysis**.
6. Choose an AI alert threshold. `75` is a reasonable starting point.
7. Choose how many shortlisted listings may be analyzed per scan. `5` keeps usage controlled.
8. Optionally leave **Let AI inspect listing photo** enabled.

The program uses OpenAI's Responses API and requests structured JSON output. Requests set `store=false`.

## How AI scoring works

The watcher does **not** send every Marketplace result to AI.

1. Marketplace listings are collected from the visible search results.
2. Local keyword, price, discount, and median-baseline rules calculate a rule score.
3. Only promising new/changed listings enter the AI shortlist.
4. AI receives the listing title/text, asking price, local baseline, rule score, and optionally the listing image.
5. AI returns a structured assessment:
   - eligible / skip
   - deal score 0–100
   - confidence 0–100
   - verdict
   - estimated value range
   - positives
   - red flags
   - short summary
6. Discord only gets the listing when it clears your configured AI score threshold.

AI pricing is an estimate, not a guarantee. Verify expensive purchases and inspect items before paying.

## Keeping API cost controlled

- AI only sees shortlisted listings.
- `AI checks / scan` caps requests.
- Unchanged listings are cached and not analyzed again.
- Turning off **Alert on first scan** makes the first run learn existing listings without AI calls.
- `gpt-5-mini` is the default model because this is a high-volume classification/ranking style task.

## Security / privacy

- `config.json` is ignored by Git.
- The Facebook browser profile is ignored by Git.
- SQLite databases are ignored by Git.
- The OpenAI API key is saved using Windows Credential Manager, not a plaintext repo file.
- OpenAI API requests use `store=false`.
- Do not publish your Discord webhook or browser profile.

## Important limitations

Facebook can change Marketplace's HTML, login flow, or access rules. If the browser clearly shows listings but the watcher finds zero, the extraction selectors may need an update.

This project does not include CAPTCHA bypassing, login bypassing, stealth plugins, proxy rotation, or code intended to defeat Facebook security controls.
