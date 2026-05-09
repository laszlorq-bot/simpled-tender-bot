# Simpled Tender Bot — Setup Guide

Runs every morning at 8 AM UK time. Scans Find a Tender for new opportunities matching Simpled's profile. Emails you a digest of matches, sorted by priority.

---

## What you need before starting

- A GitHub account (free) → github.com
- Your Anthropic API key → console.anthropic.com
- A Gmail address to send from (can be a spare one you create)
- A Gmail App Password (not your normal password — see step 3)

---

## Step 1 — Upload this folder to GitHub

1. Go to github.com and sign in
2. Click **New repository** (top right, green button)
3. Name it `simpled-tender-bot`
4. Set it to **Private**
5. Click **Create repository**
6. On the next screen, click **uploading an existing file**
7. Drag and drop ALL files from this folder (including the `.github` folder)
8. Click **Commit changes**

---

## Step 2 — Add your API keys as Secrets

In your GitHub repo:

1. Go to **Settings** (top tab)
2. Click **Secrets and variables** → **Actions** (left sidebar)
3. Click **New repository secret** and add each of these:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your key from console.anthropic.com |
| `SENDER_EMAIL` | The Gmail address the bot sends from |
| `SENDER_PASSWORD` | Your Gmail App Password (see Step 3) |
| `RECIPIENT_EMAIL` | Where you want to receive the emails |

---

## Step 3 — Create a Gmail App Password

You can't use your normal Gmail password. You need a one-time App Password:

1. Go to myaccount.google.com
2. Click **Security** (left sidebar)
3. Under "How you sign in to Google", click **2-Step Verification** (enable it if not already on)
4. Scroll to the bottom and click **App passwords**
5. Select app: **Mail** / Select device: **Other** → type "Simpled Tender Bot"
6. Click **Generate** — copy the 16-character password
7. Paste it as the `SENDER_PASSWORD` secret in Step 2

---

## Step 4 — Test it manually

1. In your GitHub repo, click **Actions** (top tab)
2. Click **Simpled Daily Tender Scan** (left sidebar)
3. Click **Run workflow** → **Run workflow** (green button)
4. Watch the logs — you should see it scanning and either sending an email or printing "No matches today"

---

## After that — it runs itself

Every morning at 8 AM UK time (9 AM Morocco/Spain time), GitHub automatically runs the scan. You'll get an email if there are matches. If there are no matches that day, no email is sent.

To scan a larger window (e.g. catch up on a week), run manually and set "How many days back" to 7.

---

## Adjusting what it looks for

Open `tender_bot.py` in GitHub and edit the `COMPANY_PROFILE` section at the top to change what counts as a good match. You can also add or remove CPV codes in the `CPV_CODES` list.

---

## Cost

- GitHub Actions: **Free** (well within free tier limits)
- Anthropic API: roughly **£0.01–0.05 per day** depending on how many tenders it finds to analyse
- Gmail: **Free**
