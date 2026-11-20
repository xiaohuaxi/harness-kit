# Google AI Subscription Perks: Claim, Configure & Verify (Agent Execution Guide)

> Note for human readers: You do not need to run these steps manually. Hand this file to your Google Antigravity (`agy` desktop app or CLI) session and say: "Check, claim, configure, and verify my Google AI subscription perks following this guide."

## 1. Mission & Entry Rules (For the Executing Agent)

You are reading an end-to-end operational guide written for an AI Agent.
- If the user only asks you to read, explain, review, or audit this guide for safety, summarize what it does, which local paths it touches, and its permission boundaries without running commands or modifying any files.
- If the user asks you to check, claim, or configure their subscription perks (or sends you this file directly to execute), take full ownership and walk them through all 7 active bundled perks plus the June 2026 sunset corrections: local read-only discovery, claiming and activating credits, safety caps, official CLI/skill installation, and live verification.

Host compatibility:
- Default environment: Google Antigravity (`Antigravity App` desktop or `agy` CLI).
- Other Agent hosts (`Claude Code`, `Codex`, etc.): Everything below applies identically except Stage 1 (browser access), where you should use your host's browser tool (such as `chrome-devtools` or `playwright` MCP). If your current session has no browser tool or runs non-interactively, send the exact URLs and checklist items to the user to inspect while you handle all local CLI steps.

## 2. Interaction Discipline & Safety Red Lines

- Announce before acting: Before entering each new stage, tell the user in 2 to 3 clear sentences (in their current language) what you are about to inspect or change and whether it involves any cost. Never run a long sequence of opaque actions without warning.
- Read-only idempotent checks first: Always inspect current state before changing anything. If the user already has an active Cloud Billing account, current month's credits applied, or `colab` installed and authorized, fill in only missing settings (such as auto-apply) or move straight to verification. Never claim twice. Before editing any local config file, check whether it already exists and merge keys rather than overwriting the file.
- Red line (mandatory human-in-the-loop checkpoints):
  1. Entering Google credentials, completing 2FA / Passkey, or solving a CAPTCHA;
  2. Entering credit/debit card details or billing address on Google Cloud Billing, or confirming any real payment (such as initial AI Studio prepay);
  3. Pasting the one-time `colab` OAuth code in the user's own interactive terminal, or typing `/browser` in the `agy` CLI prompt and clicking `Allow` in Chrome;
  4. Creating or purchasing any resource that incurs real billable usage.
  At every checkpoint above, leave the relevant page open, tell the user the exact step needed, and wait for confirmation before continuing.
- Red line (credential secrecy): Newly created or existing `GEMINI_API_KEY` values, billing account IDs, and OAuth codes may only be written to user-approved local private files (`chmod 600`). Never echo full secrets in chat, never pass secrets on process command lines where `ps` can expose them (such as literal `-H`), and never write secrets into Git-tracked files.
- Red line (GPU billing isolation & protecting active jobs): Bundled GPU quota applies strictly to Google Colab Compute Units (CU). Never spin up a GPU VM on Google Cloud Compute Engine (GCP bills `$3.67+/hr` directly to your Cloud Billing account, not Colab CU). Before launching a Colab smoke test, always list existing active sessions first, name your test session uniquely, and tear down only that test session afterward. Never stop the user's existing training or inference jobs.

## 3. Stage 0: Local Read-Only Discovery & Intake

1. Run zero-side-effect local checks (continue cleanly if a tool is missing; never read or print environment secret values or key files):

```bash
uname -s
command -v python3 uv pipx node npx || true
command -v colab || test -x "$HOME/.local/bin/colab" && echo "colab_found" || true
CHROME_MAC="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
test -x "$CHROME_MAC" && "$CHROME_MAC" --version || true
command -v google-chrome && google-chrome --version || true
```

2. Report local tool availability concisely (and remind the user they can run `/usage` inside `agy` to confirm they are signed in with the primary Pro or Ultra subscription account). Ask only for intake details the user has not already shared:
   - Which tier is active: `Google AI Pro ($20/mo)`, `Ultra 5x ($100/mo)`, or `Ultra 20x ($200/mo)`? Confirm this is the primary subscriber account (not a family-group member, free trial, or student plan), and whether it was billed directly via Google or through the Apple App Store / third-party store.
   - Does the account already have an active Google Cloud Billing account, or does the user have a physical international `Visa / Mastercard / Amex` card if a new billing account is needed?
     (Routing rule: If an existing Cloud Billing account is present, check whether the Developer Program benefits page can bind to it directly. If the user has no Cloud Billing account and no eligible physical card, or subscribed via Apple App Store billing that cannot sync developer credits, explain clearly and **skip Stage 2**, jumping straight to Stage 3 for the 6 perks that require no Cloud Billing account.)
   - If claiming Cloud & Gemini API credits, where do they plan to use them: their own AI apps/scripts, a 24/7 Personal Bot, a headless remote server running `agy`, or strictly Google Cloud services (skipping Gemini API)?

## 4. Stage 1: Connect Browser Access

Check your declared tools and connect to the browser accordingly (you may run Stage 3's local CLI installation while waiting for a user handshake):

- Mode A: Your session already has a built-in `browser` tool (such as `Antigravity App` desktop `v2.30.0+` or an `agy` session after `/browser` handshake).
  Open target URLs directly with the built-in `browser` tool (no external browser skill needed). If the browser is not signed in to the primary subscriber Google account, pause and let the user sign in before continuing.

- Mode B: Running in interactive `Antigravity CLI` (`agy`) without a mounted `browser` tool yet.
  `agy` includes a native `/browser` slash command (launching official Chrome DevTools MCP via `npx`, no extra skill required) if Node.js is installed and Google Chrome is `v144+`:
  1. If Stage 0 found no Node.js, no Chrome, or the session is non-interactive (`--print`), switch to "Browser Guide Mode": give the user each exact URL and field to check, collect their replies, and handle all local CLI steps yourself.
  2. Otherwise, guide the user through a one-time two-step handshake:
     - In Chrome (signed in to the primary Google subscriber account), open:
       `chrome://inspect/#remote-debugging`
       and enable `Allow remote debugging for this browser instance`;
     - In the `agy` prompt, send `/browser Continue checking and configuring my Google AI subscription perks following the guide`, then click `Allow` in Chrome within 60 seconds.

## 5. Stage 2: Claim Monthly `$10 / $40 / $100` Cloud & Gemini API Credits

(Perk 1: Google Cloud & Gemini API monthly credits. Skip to Stage 3 if Stage 0 confirmed no eligible Cloud Billing setup or third-party store billing blocks syncing.)

### 5.1 Link Billing Account & Enable Auto-Apply

1. Open the Google Developer Program benefits page and verify you are signed in as the primary subscriber (family members do not receive this credit):
   `https://me.developers.google.com/benefits`
2. Locate the `Google Cloud credits` card (`$10/mo` on Pro, `$40/mo` on Ultra 5x, `$100/mo` on Ultra 20x). Each grant stays valid for 365 days from issuance, renews monthly on your activation anniversary date, and unused balances stack across months.
3. Follow the actual page state:
   - Already linked and `Always use this billing account` is checked: record status and proceed to `5.2`.
   - Linked, but `Always use this billing account` is **unchecked**: check it and save (leaving it unchecked strands future monthly or upgrade grants unclaimed on the benefits page), then proceed to `5.2`.
   - Not yet claimed or no billing account linked:
     * If an eligible Google Cloud Billing account already exists, select it and check `Always use this billing account`.
     * If no Cloud Billing account exists, click to create one and **pause immediately** on the payment profile screen. Let the user enter their physical international credit/debit card details (Google Cloud Billing generally rejects UnionPay-only cards, most virtual/prepaid cards, and Apple Pay; billing country must match the card issuer, and a temporary `$1` authorization hold may appear). Resume once the user confirms creation, then finish linking and checking auto-apply.

### 5.2 Verify Credit Balance in Cloud Billing

Open Cloud Billing, enter the linked billing account, and check `Credits` in the left navigation:
`https://console.cloud.google.com/billing`

Verify that the current month's grant (`$10 / $40 / $100`, or the equivalent amount in the billing account's local currency) is active, and record total available balance plus earliest expiration date. If the user specified in Stage 0 that they only want credits for GCP services (not the Gemini API), Stage 2 is complete: skip `5.3` and jump to Stage 3.

### 5.3 Check AI Studio Billing Mode, Safety Caps & Generate API Key

1. Open `https://aistudio.google.com/app/apikey` and its Billing page, and check the billing mode for the linked Cloud Billing account:
   - `Prepay` mode (not yet activated): Enabling paid tier for the first time typically requires a small initial prepay (usually `$5.00`). **Pause immediately** on any payment confirmation screen for the user to complete. Explain clearly that once activated, Gemini API calls draw from monthly Cloud credits **first**, leaving the `$5.00` cash prepay untouched as long as usage stays within the credit balance.
   - `Postpay` mode (older billing accounts): No `$5` initial prepay is needed; monthly Cloud credits apply automatically. Overages still bill to the card on file at month-end, so configure the cap below.
2. Configure safety limits (note: these constrain AI Studio Gemini API calls only, not other GCP services):
   - If `Auto-reload` is shown on the AI Studio Billing page, confirm it is turned **Off**;
   - With user confirmation, set a monthly `Spend Cap` aligned with their available credit balance (usage metering has a 10+ minute lag, so `Auto-reload Off` plus `Spend Cap` gives the strongest protection against accidental charges; if the user also runs Cloud Run or other GCP services, remind them to set a Budget alert in Cloud Billing).
3. If no usable key exists yet, create a `GEMINI_API_KEY` under a GCP project linked to that same Cloud Billing account. Before saving, check whether the target private file already exists (to avoid overwriting an existing key), confirm with the user, and save with `chmod 600` (for example `~/.config/gemini/api_key`).

### 5.4 Sub-`$0.0001` Live API Smoke Test

Pass the API key header via stdin (never in process CLI arguments) and test against the active lightweight model `gemini-3.8-flash` (replace `KEY_FILE` if the user chose a different path):

```bash
KEY_FILE="$HOME/.config/gemini/api_key"
BASE="https://generativelanguage.googleapis.com/v1beta"
URL="$BASE/models/gemini-3.8-flash:generateContent"
BODY='{"contents":[{"parts":[{"text":"Reply OK"}]}]}'
OPTS=(-sS -w "\nHTTP_STATUS:%{http_code}\n" -H "Content-Type: application/json")
printf 'x-goog-api-key: %s\n' "$(tr -d '\r\n ' < "$KEY_FILE")" |
  curl "${OPTS[@]}" -H @- -d "$BODY" "$URL"
```

Confirm `HTTP_STATUS:200` and `OK` in the response body. Billing credit deductions typically appear with a delay of tens of minutes, so report both facts accurately: API key verified live, and GCP project confirmed linked to the credited billing account.

### 5.5 Four Practical Destinations (And `agy` Auth Exclusivity)

- Destination 1 (Your own AI apps & scripts): Read `GEMINI_API_KEY` from the private `600`-permission file into your app or script runtime.
- Destination 2 (24/7 Personal Bot): Power an always-on personal assistant running scheduled summaries, daily Q&A, or automated workflows.
- Destination 3 (Headless remote server running `agy`): **Critical boundary**: `agy` personal Google login mode **does not support** Bring-Your-Own-Key (BYOK) for extra rate limits. **Never** write `"modelProvider": "gemini"` into `~/.gemini/antigravity-cli/settings.json` on a personal machine where you rely on your Google subscription login, or it will bypass your subscription session. Use API-key mode only on remote/headless servers where signing in with a personal Google account is impractical:
  1. Merge `"modelProvider": "gemini"` into the top-level JSON object of remote `~/.gemini/antigravity-cli/settings.json` (preserve all existing keys; never overwrite the file);
  2. Export `GEMINI_API_KEY` on the remote host from a `chmod 600` private file (not in shell history).
- Destination 4 (Lightweight GCP services): The same monthly credit balance automatically offsets `Vertex AI`, `Cloud Run`, and `Cloud Storage + Cloud CDN` usage on that billing account.

## 6. Stage 3: Install Official Colab CLI & Skill, Verify GPU Compute Units

(Perk 2: Colab Compute Units `200 / 1,000 / 2,000 CU`. Requires no Cloud Billing account. Primary subscriber account only; family members, free trials, and student plans are ineligible.)

Specifications & live benchmark baseline:
- `Pro ($20/mo)`: `200 CU/mo` (`~$20` retail at `$9.99 / 100 CU`), no background execution after closing the tab;
- `Ultra 5x ($100/mo)` / `Ultra 20x ($200/mo)`: `1,000 CU/mo` / `2,000 CU/mo` (`~$100` / `$200` retail), eligible for `H100 / G4 / A100 / L4 / T4` and `TPU (v6e1 / v5e1)` subject to inventory, and both support **background execution up to 24 hours** after closing the browser tab;
- Live `40GB A100` benchmark: `275.8 TFLOPS BF16` at a real-time burn rate of `6.73 CU/hr` (roughly 20% less runtime than the commonly circulated `5.38 CU/hr` estimate), yielding roughly `30 / 150 / 300 hours` of A100 time per month for training, fine-tuning, or running open-weight models and experiments.

### 6.1 Install Official `google-colab-cli` (macOS & Linux)

Windows users can run this inside WSL2. On native Windows or machines without `uv`/`pipx`, have the user check remaining Compute Units directly in the top-right Resources panel at `https://colab.research.google.com`. When `uv` or `pipx` is available:

```bash
# Prefer uv (fallback: pipx install google-colab-cli)
uv tool install google-colab-cli
```

Locate the binary with `COLAB_BIN="$(command -v colab || echo "$HOME/.local/bin/colab")"` and run `"$COLAB_BIN" version` (remind the user to add `~/.local/bin` to `PATH` if needed).

### 6.2 Export & Mount the Bundled Official `colab-operator` Skill

`google-colab-cli` ships an official Agent skill. Both `Antigravity App` and `agy` CLI read user-level skills from `~/.gemini/config/skills/` (do not install only under `antigravity-cli/skills/`, which the desktop App ignores). Write atomically via a temp file so a failed command never truncates an existing file:

```bash
COLAB_BIN="$(command -v colab || echo "$HOME/.local/bin/colab")"
# Shared by Antigravity App & agy CLI (use ~/.agents/skills for Codex, ~/.claude/skills for Claude Code)
SKILL_DIR="$HOME/.gemini/config/skills/colab-operator"
mkdir -p "$SKILL_DIR"
TMP_SKILL="$(mktemp)"
"$COLAB_BIN" skill > "$TMP_SKILL"
test -s "$TMP_SKILL" && mv "$TMP_SKILL" "$SKILL_DIR/SKILL.md"
```

(If `SKILL.md` already exists with different content, ask the user before replacing it.)

### 6.3 First-Time Interactive OAuth & Zero-Cost Balance Check (Required)

1. Check whether local OAuth credentials exist (existence check only; **never** read the file contents):

```bash
test -f "$HOME/.config/colab-cli/token.json" && echo "HAS_TOKEN" || echo "NO_TOKEN"
```

2. **Anti-hang rule**: Current `google-colab-cli` defaults to `--auth oauth2` remote paste flow (it prints an auth URL and blocks on terminal `stdin` waiting for the pasted code rather than running a localhost callback server). If `NO_TOKEN` is returned (or a later command returns `401`), **do not run it in a background shell where it will hang**. Instead, ask the user to run the binary path discovered in `6.1` once in their own terminal (for example `~/.local/bin/colab usage`), open the printed Google authorization link in their browser, approve access, and paste the authorization code back into that terminal (the code never passes through chat and writes `~/.config/colab-cli/token.json` automatically).
3. Once credentials exist, run read-only verification:

```bash
COLAB_BIN="$(command -v colab || echo "$HOME/.local/bin/colab")"
"$COLAB_BIN" usage
"$COLAB_BIN" sessions
```

Record `Current balance` (each CU grant is valid for 90 days, so rollover can exceed the monthly allotment), `Usage rate`, and any pre-existing active sessions.

### 6.4 Optional: 10-Second Named GPU Smoke Test (~`0.02 CU`)

Ask the user whether they want to spin up a cloud GPU for a 10-second verification run. Only proceed with explicit approval:
1. Record all pre-existing active sessions first (protecting any running jobs);
2. Use the dedicated session name `agy-perk-smoke` (**never** pass `--keep`):

```bash
COLAB_BIN="$(command -v colab || echo "$HOME/.local/bin/colab")"
SMOKE_PY="$(mktemp /tmp/colab_gpu_smoke.XXXXXX.py)"
echo 'import subprocess as s' > "$SMOKE_PY"
echo 'print(s.check_output(["nvidia-smi", "-L"], text=True))' >> "$SMOKE_PY"
"$COLAB_BIN" run -s agy-perk-smoke --gpu A100 "$SMOKE_PY"
rm -f "$SMOKE_PY"
```

(If `A100` inventory is temporarily unavailable, run `"$COLAB_BIN" sessions` first to confirm no `agy-perk-smoke` session remains, then retry with `--gpu L4` or `--gpu T4`.)

Mandatory teardown audit (red line, always run immediately after):

```bash
COLAB_BIN="$(command -v colab || echo "$HOME/.local/bin/colab")"
"$COLAB_BIN" sessions
"$COLAB_BIN" usage
```

- Pass criteria: `agy-perk-smoke` is no longer in the active sessions list (if it still appears, immediately run `"$COLAB_BIN" stop -s agy-perk-smoke` to terminate only the smoke session; confirm `Usage rate: 0.00/hr` if the account had no prior jobs, or that pre-existing jobs remain intact and the burn rate returned to baseline).

## 7. Stage 4: Verify the Remaining 5 Active Perks & Sunset Corrections

None of the perks below require a Google Cloud Billing account. Inspect them read-only in the browser and report status plus key boundaries:

1. Perk 3: `YouTube Premium` (`Premium Lite` individual on Pro / full `YouTube Premium Family` on Ultra 5x & 20x) and region/hardware extras
   - Check membership status on both pages (availability depends on account region):
     `https://www.youtube.com/paid_memberships`
     `https://one.google.com/benefits`
   - Tier & conflict rules: Eligible paid `Pro` primary accounts without an existing standalone YouTube plan automatically activate `YouTube Premium Lite` (ad-free on most non-music videos, no YouTube Music, non-shareable). On `Ultra 5x / 20x` in supported regions, Google automatically upgraded the perk to the full **`YouTube Premium Family` plan** (site-wide ad-free, background play, offline downloads, YouTube Music, shareable with up to 5 family members for 6 total). If the account already has a separate paid YouTube plan preventing activation, **report the conflict neutrally and never push the user to cancel an existing subscription**.
   - Hardware & region extras: Ultra also includes `Project Genie` (Genie 3 real-time 3D worlds, 60s sessions), region-gated `Chrome auto-browse` / calling experiments, `Google Home Premium` (requires Nest hardware), and `Fitbit / Health Premium` (requires Pixel Watch / Fitbit); note these only if relevant to the user's region/hardware.

2. Perk 4: `Google One Storage` (`5 TB / 20 TB / 30 TB` with zero egress fees)
   - Check total storage and up to 6-member (primary + 5) family sharing settings:
     `https://one.google.com/storage`
     `https://one.google.com/settings`
     (Note: Drive storage, consumer AI, and Ultra YouTube Family can be shared with the family group; Cloud/API credits and Colab CU remain exclusive to the primary subscriber.)
   - Practical workflows & 403 limit: (a) Mount Drive directly inside Colab (`from google.colab import drive; drive.mount('/content/drive')`, or run `colab drivemount -s <name>` manually in an interactive local terminal; note `colab drivemount`, `colab repl`, and `colab console` require an interactive TTY so **Agents must never invoke them in background shells**) to read/write 100GB+ weights and datasets over Google's internal network; (b) Archive 4K/8K raw footage or run scheduled encrypted `rclone` backups; (c) **Never** use Drive public links to host public web video streams or media CDN, as concurrent playback quickly triggers a 24-hour `HTTP 403` lockout (use Stage 2 Cloud credits on `Cloud Storage + Cloud CDN` instead).

3. Perk 5: `Veo Video Generation` (`Google Flow`, `1,000 / 10,000 / 25,000 credits/mo`)
   - Check monthly credit balance in the top-right corner (web-only; no official CLI):
     `https://labs.google/fx/tools/flow`
   - Per 8-second clip with audio: `Veo 3.1 Quality` costs `100` credits, `Omni Flash 1.2` costs `24` credits, and `Veo 3.1 Fast` costs `20` credits on Pro / `10` on Ultra, giving `10 to 50` clips/mo on Pro (`1080p` max), `100 to 1,000` on 5x, and `250 to 2,500` on 20x. `5x` and `20x` support `4K` output (`+50` credits per 1 to 10s clip).

4. Perk 6: `AI Music Generation` (`Google Flow Music`, `10,000 / 30,000 / 30,000 credits/mo`)
   - Separate service and independent credit pool from Flow video. Check balance at:
     `https://flowmusic.app`
   - `10,000` credits yield roughly `2,000` songs and `30,000` yield roughly `6,000` songs (`~5 credits/song`), powered by `Lyria 3.5` with vocal/instrumental generation, stems, and royalty-free commercial license (web-only).

5. Perk 7: `NotebookLM` (`300 / 500 / 600` sources per notebook)
   - Check upgrade badge next to avatar or click `Add sources` in any notebook:
     `https://notebooklm.google.com`
   - Expected limits: Free is `50` sources; `Pro` is `300`; `Ultra 5x (20 TB)` is `500` (`600` for note sources); `Ultra 20x (30 TB)` is `600`. All three paid tiers support up to `500` notebooks.

### Sunset Corrections (June 2026)

Inform the user that two perks still listed in older community posts were **retired in June 2026**:
- "30 Firebase Studio workspaces" (new workspace creation ended June 22, 2026);
- Consumer "Gemini Code Assist IDE plugin / legacy Gemini CLI" for personal, Pro, and Ultra accounts (ended June 18, 2026).

Both have consolidated into `AI Studio` and `Antigravity` (VS Code / JetBrains extensions available), so there is no need to hunt for them.

## 8. Stage 5: Deliver the Full Perks Verification Summary

Once all checks and configurations finish, present a concise summary table covering all 7 active perks plus optional extras:
1. `Cloud & Gemini API Credits` (Developer Program link & auto-apply checkbox, credit balance & earliest expiry, Prepay/Postpay mode, `Auto-reload` & `Spend Cap` status, API key smoke test & saved path);
2. `Colab GPU Compute Units` (`google-colab-cli` & `~/.gemini/config/skills/colab-operator` status, remaining `CU` balance, smoke test teardown confirmation);
3. `YouTube Premium & Extras` (active Lite / Family plan status, region/hardware notes, or existing-plan conflict);
4. `Google One Storage` (`5 TB / 20 TB / 30 TB` total capacity & family sharing status);
5. `Veo Video (Google Flow)` (monthly video credit balance);
6. `AI Music (Google Flow Music)` (monthly independent music credit balance);
7. `NotebookLM` (tier badge & per-notebook source limit: `300 / 500 / 600`).
