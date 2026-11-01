# Jalmitra Telegram Bot — GitHub Actions setup

This repository packages the supplied Jalmitra bot for a Linux GitHub Actions runner. The original Python modules are retained; Windows-only file paths and hard-coded credentials have been replaced with environment-based configuration.

## 1. Rotate credentials before publishing

The uploaded source archive contained a Telegram bot token and Rajasthan SSO credentials in `config.py`. Treat those credentials as exposed:

- Revoke the old Telegram bot token with **@BotFather** and generate a new one.
- Change the Rajasthan SSO password if it is still valid.
- Do not commit credentials, cookies, OTPs, or portal session files to GitHub.

## 2. Create the repository

1. Create a new GitHub repository (prefer **Private**).
2. Upload the contents of this folder to the repository root. The `.github/workflows/run-bot.yml` path must be preserved.
3. Open **Settings → Secrets and variables → Actions → New repository secret** and add:
   - `BOT_TOKEN` — the newly generated Telegram bot token (required)
   - `SSO_USER` — Rajasthan SSO user ID
   - `SSO_PASS` — Rajasthan SSO password
4. Save the secrets. Never put these values in the Python files or workflow YAML.

## 3. Start it

1. Open the repository's **Actions** tab.
2. Select **Run Jalmitra Telegram Bot**.
3. Click **Run workflow** and type `RUN` in the confirmation field.
4. Open the running job to view logs. Do not print credentials, cookies, or OTPs in logs.

The workflow uses polling and runs on a GitHub-hosted Ubuntu machine. It is manually started; it is not an always-on hosting service. GitHub-hosted jobs have a maximum execution time and this workflow is configured to stop before the six-hour limit. When the job stops, the Telegram bot goes offline. Start it again from Actions when needed.

## 4. Important runtime limitations

- GitHub-hosted runners are temporary. Files under `runtime_data/` and downloaded documents are lost when the job ends. Cookie persistence and saved files therefore do not survive a new run unless a separate secure persistent storage solution is implemented.
- Wallet refill and SSO automation use Playwright Chromium. The workflow installs Chromium and its Linux dependencies automatically.
- CAPTCHA, OTP/TOTP, payment gateway, and portal flows may require interactive steps or may be blocked by the portal when running from a cloud IP. GitHub Actions cannot display a normal desktop browser to you.
- Do not use GitHub Actions as a way to bypass portal security, CAPTCHA, OTP, payment confirmation, or access controls. Complete all required verification and payment approvals yourself.
- GitHub Actions usage is subject to your account's included minutes and billing limits. A six-hour run is not guaranteed to be free.

## 5. Local run (optional)

Use Python 3.11, install `requirements.txt`, install Chromium with `python -m playwright install --with-deps chromium`, set the same environment variables, and run `python main.py`.
