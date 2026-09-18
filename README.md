# Inbox Cleanup: Local Gmail Dashboard

Stop the subscriptions you never open, trash their old emails, and label what's left. Every action is previewed first, and everything except an unsubscribe can be undone.

## What This Does

This app runs on your computer and helps you clean up your inbox in four steps:

1. **Find subscriptions you don't open.** Scans your Gmail to identify senders with an unsubscribe option (like newsletters and promotions) that you rarely or never open.

2. **Stop them.** Unsubscribes from the ones you choose and creates a Gmail filter that sends their future emails straight to Trash. Both actions are optional. The filter can be undone; an unsubscribe cannot.

3. **Trash their old emails.** Moves old messages from those senders to Trash (not permanently deleted; Gmail keeps it for 30 days). Skips any emails you have starred.

4. **Label what's left.** Uses AI to sort your remaining inbox into folders like Finance, Travel, Personal, and Work.

**Everything is report-first and reversible.** Before any action, you see exactly what will happen. Trash is recoverable for 30 days. Filters can be removed. Labels can be deleted.

## Requirements

- Python 3.11 or newer
- A Google account with Gmail enabled
- (Optional) An Anthropic API key, only needed if you want the AI labeling step (Organize)

If you don't have an Anthropic key, the app still works in "rules-only" mode: you can scan, stop subscriptions, and trash emails. Labeling just won't run.

## Quick Start

1. Clone or download this project folder.

2. Open Terminal (Mac/Linux) or PowerShell (Windows) and navigate to the project folder.

3. Run the launcher in demo mode first, so you can explore with example data and no Gmail access:
   - Mac/Linux: type `./run.sh --demo` and press Enter
   - Windows: type `powershell -ExecutionPolicy Bypass -File run.ps1 -Demo` and press Enter

4. A browser window opens at http://127.0.0.1:8765 with a DEMO banner and a synthetic mailbox. Try a scan, the filters, and a Stop or Trash action; nothing leaves your computer.

5. To use it with your real Gmail:
   - Follow the setup steps in [docs/SETUP.md](docs/SETUP.md) to get `credentials.json` and your API key
   - Start the launcher without the demo flag (`./run.sh` or `run.ps1`)
   - In the browser, go to Settings and click "Sign in with Google"
   - Then go to Overview and click Scan

## How It Works

The app runs in four stages:

**Scan.** Reads the headers of every email in your Gmail (within a time window like "last 12 months"). Counts how many emails you got from each sender, how many you opened (vs. just received), and when you last opened one. Does not download full emails or change anything. May take 15 to 30 minutes for a large mailbox because Gmail limits how fast the app can read.

**Subscriptions.** Lists every sender with the numbers that matter, and lets you filter by:
- How many emails they sent you (default: at least 3)
- What percent of them you opened (default: 10% or less)
- When you last opened one (default: 90 or more days ago, or never)
- Whether their emails carry an unsubscribe header

All of these are adjustable on the tab. Select the senders you want gone and choose Stop (unsubscribe and/or block with a filter), Trash older than N days, or Keep.

**Organize.** Classifies your remaining inbox into categories like Travel, Finance, Work, Personal, etc. Uses the cheapest capable AI model for bulk work (re-checks low-confidence guesses with a stronger model). Shows you the estimate before running. You can skip any email or override the category.

**Activity.** A log of everything the app has done, so you can undo actions (except unsubscribes, which Gmail doesn't allow).

## Cost

AI classification costs money. Here is what to expect:

- **Haiku 4.5** (fast, cheap model) processes most emails at **$1 per million input tokens**, roughly $0.10 to $0.30 per 1,000 emails.
- **Sonnet 5** (stronger model) rechecks low-confidence results at **$2 per million input tokens**.

Before you run the Organize step, the app shows a cost estimate. You can review it and decide whether to proceed. The estimate appears on the Organize tab.

If you don't have an Anthropic API key, labeling will not run, but everything else (scan, stop subscriptions, trash) works fine.

## Safety

- **Trash, not delete.** Emails go to Gmail Trash, where they stay for 30 days. You can recover them anytime.
- **Filters can be removed.** If you block a sender and change your mind, click Undo on the Activity tab, or delete the filter in Gmail under Settings, then "Filters and Blocked Addresses".
- **Unsubscribes cannot be undone.** Once you unsubscribe, the sender's list removes you. You'll need to re-subscribe manually if you change your mind.
- **Starred emails are skipped.** The app never trashes anything you have starred.
- **Important emails are skipped by default.** When you trash old emails, the app skips Gmail's "Important" label. You can turn this off in Settings.
- **Your files stay on your computer.** `credentials.json` and `token.json` (your Gmail credentials) and `.env` (your API key) never leave your machine and are in `.gitignore`.

## Troubleshooting

**"credentials.json not found"**

You have not created an OAuth client in Google Cloud Console. Follow [docs/SETUP.md](docs/SETUP.md) from the top, especially step E.

**"Access blocked: This app isn't verified"**

Google shows this warning for unverified apps. It is safe for you (it is your own app, running on your own computer). Click "Advanced" → "Go to Inbox Cleanup (unsafe)". Then add yourself as a test user in Google Cloud Console (see docs/SETUP.md, step C).

**"Rate limit"**

Gmail limits how fast you can read emails. The app automatically slows down and retries. This is normal and harmless for the first scan.

**"No ANTHROPIC_API_KEY"**

You have not set your Anthropic API key in `.env`. The Organize step (AI labeling) will not run, but everything else works. See [docs/SETUP.md](docs/SETUP.md), step F, to add your key.

**"Port 8765 already in use"**

Another app is using that port. Open `.env` and change `PORT=8765` to a different number like `PORT=8766`, then run the launcher again.

## Development

To run tests:

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

To run with demo data (synthetic mailbox, no Gmail access):

```bash
./run.sh --demo
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full system design, database schema, and API routes.

