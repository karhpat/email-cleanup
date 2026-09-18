# Connect the App to Your Gmail

This guide walks you through connecting the app to your real Gmail account. It takes about 10 minutes and happens mostly in Google Cloud Console, which is Google's developer portal. You do not need to know how to program.

These steps match Google Cloud Console as of September 2026. Google moves buttons around from time to time, but the flow stays the same: create a project, turn on the Gmail API, tell Google who the app is, add yourself as a tester, and download one file.

## Checklist before you start

- Your Gmail address and password
- The project folder on your computer (the one that contains `README.md`)
- About 10 minutes
- Optional: an Anthropic account, only if you want the AI labeling step

## Step A: Create a Google Cloud project

1. Open [console.cloud.google.com](https://console.cloud.google.com) and sign in with your Gmail account.
2. At the top of the page, click the project picker. It shows "Select a project" or the name of a project.
3. Click "New project".
4. For "Project name", type `Inbox Cleanup`.
5. Click "Create". Wait a few seconds, then make sure the picker at the top now shows "Inbox Cleanup".

## Step B: Turn on the Gmail API

1. Click the three-line menu at the top left, then "APIs & Services", then "Library".
2. In the search box, type `Gmail API` and press Enter.
3. Click "Gmail API" in the results.
4. Click "Enable".

## Step C: Tell Google about the app and add yourself as a tester

Google needs an "OAuth consent screen" for every app. Because this app is private to you, it stays unverified, and Google only lets listed test users sign in. That is fine: the only test user is you.

1. Click the three-line menu, then "APIs & Services", then "OAuth consent screen".
2. If you see a page titled "Google Auth Platform" with a "Get started" button, click "Get started". If you instead see an older form that asks "External" or "Internal", choose "External" and click "Create"; the same fields below appear in the same order.
3. App information: for "App name" type `Inbox Cleanup`, and for "User support email" pick your Gmail address. Click "Next".
4. Audience: choose "External". Click "Next".
5. Contact information: type your Gmail address. Click "Next".
6. Tick the box to agree to Google's policy, click "Continue", then click "Create".
7. Now add yourself as a tester. In the left menu of Google Auth Platform, click "Audience".
8. Scroll to "Test users" and click "Add users".
9. Type your Gmail address and click "Save".

Leave the "Publishing status" as "Testing" for now. See "Signing in again every 7 days" near the end of this guide for the trade-off.

## Step D: Add the two permissions (scopes)

Scopes are the permissions the app will ask you for when you sign in.

1. In the left menu of Google Auth Platform, click "Data Access". (In the older wizard, this is the "Scopes" step; click "Edit app" and press "Save and continue" until you reach it.)
2. Click "Add or remove scopes".
3. Scroll down to the box labeled "Manually add scopes" and paste these two lines, one per line:

   ```
   https://www.googleapis.com/auth/gmail.modify
   https://www.googleapis.com/auth/gmail.settings.basic
   ```

4. Click "Add to table".
5. Click "Update" at the bottom of the panel.
6. Click "Save".

Google will show a note that these are sensitive or restricted scopes and that a public app would need verification. Ignore it; a private app in Testing with you as the only tester does not need verification.

## Step E: Create the desktop client and download credentials.json

This produces the one file the app needs to talk to Gmail.

1. Click the three-line menu, then "APIs & Services", then "Credentials".
2. Click "Create credentials" at the top, then "OAuth client ID".
3. For "Application type", choose "Desktop app".
4. For "Name", type `Inbox Cleanup desktop`.
5. Click "Create".
6. A window shows your client ID. Click "Download JSON". The file is named something like `client_secret_1234-abcd.apps.googleusercontent.com.json`.
7. Rename that file to exactly `credentials.json`.
8. Move it into the project folder, next to `README.md`.

Keep this file private. It is already listed in `.gitignore`, so it is never uploaded if you share the project.

## Step F: Add your Anthropic API key (optional)

This key lets the "Organize" tab use Claude to label your remaining email. Skip this step if you only want to stop subscriptions and trash old mail; everything except labeling works without it.

1. Open [console.anthropic.com](https://console.anthropic.com) and sign in or create an account.
2. In the left menu, click "API keys".
3. Click "Create key", give it any name, and copy the key. It starts with `sk-ant-`.
4. In the project folder, find the file `.env`. If it is not there yet, run the app once (Step G) and it will be created for you from `.env.example`. On Mac and Windows, files that start with a dot can be hidden; in Finder press Cmd+Shift+. and in File Explorer turn on "Hidden items" under the View menu.
5. Open `.env` in any text editor.
6. Find the line `ANTHROPIC_API_KEY=` and paste your key after the equals sign, with no quotes and no spaces.
7. Make sure the line `DEMO=0` is present. `0` means live mode.
8. Save the file.

## Step G: Run the app and sign in

1. Open Terminal (Mac or Linux) or PowerShell (Windows) and go to the project folder.
2. Start the app:
   - Mac or Linux: `./run.sh`
   - Windows: `powershell -ExecutionPolicy Bypass -File run.ps1`
3. Your browser opens at http://127.0.0.1:8765. Keep the terminal window open the whole time you use the app; closing it stops the app.
4. Click the "Settings" tab.
5. Click "Sign in with Google". A new browser window opens.
6. Choose your Gmail account.
7. Google shows "Google hasn't verified this app". Click "Advanced", then "Go to Inbox Cleanup (unsafe)". The word "unsafe" only means Google has not reviewed the app; it is your own app running on your own computer.
8. Google asks what Inbox Cleanup can access. Tick both boxes:
   - "Read, compose, and send emails from your Gmail account"
   - "See, edit, create, or change your email settings and filters in Gmail"
9. Click "Continue".
10. The window says you can close it. Go back to the dashboard tab. Settings now shows your email address as signed in.

The app saved a file called `token.json` in the project folder. That file is your sign-in; it stays on your computer and is gitignored.

## Step H: Run the first scan

1. Click the "Overview" tab.
2. Choose how far back to scan. "12 months" is a good first choice.
3. Click "Scan".
4. Watch the progress bar. The app reads only email headers (sender, subject, date, read state), never full messages, and changes nothing during a scan.
5. A large mailbox can take 15 to 30 minutes because Gmail limits how fast an app may read. That is normal. You can click "Cancel" at any time and click "Scan" later to resume where it stopped.
6. When it finishes, Overview fills in with totals, and the "Subscriptions" tab lists senders you never open.

## Signing in again every 7 days

While the app's publishing status is "Testing", Google expires your sign-in after 7 days. When that happens the dashboard shows you as signed out; click "Sign in with Google" again and it works for another 7 days. Nothing is lost; your scanned data stays in the `data/` folder.

If you would rather not sign in weekly: in Google Auth Platform, click "Audience", then "Publish app", and confirm. The sign-in then lasts until you revoke it. You will still see the "unverified app" warning each time you sign in, and Google may ask you to confirm again that you trust the app. Both options are fine for personal use.

## What the permissions allow

- **Read, compose, and send emails** (`gmail.modify`): read message headers, apply labels, mark as read, move messages to Trash and back, and send email. The app sends email only when you approve an unsubscribe for a sender whose unsubscribe link is an email address. It never permanently deletes anything.
- **Email settings and filters** (`gmail.settings.basic`): create a filter that sends future mail from a sender you blocked to Trash, and delete a filter again when you undo. The app only deletes filters it created itself.

## Revoking access

1. Open [myaccount.google.com](https://myaccount.google.com).
2. Click "Security" in the left menu.
3. Scroll to "Your connections to third-party apps & services" and click it.
4. Click "Inbox Cleanup", then "Delete all connections you have with Inbox Cleanup" (the wording varies slightly), and confirm.
5. Delete `token.json` from the project folder.

Filters the app created stay in Gmail unless you undo them in the app's Activity tab or remove them in Gmail under Settings, then "Filters and Blocked Addresses".

## Where files live

| File | Where | What it is |
|---|---|---|
| `credentials.json` | Project folder, next to `README.md` | Identifies the app to Google. Private. Gitignored. |
| `token.json` | Project folder | Your sign-in, created after the first "Sign in with Google". Private. Gitignored. |
| `.env` | Project folder | Your Anthropic key and settings such as `DEMO` and `PORT`. Private. Gitignored. |
| `data/` | Project folder | The local database of scanned email headers and the activity log. Gitignored. |

## Troubleshooting

**"credentials.json not found"**
The file is not in the project folder or has a different name. Check that it sits next to `README.md` and is spelled exactly `credentials.json`.

**"Error 403: access_denied" or "This app is currently being tested and can only be accessed by developer-approved testers"**
Your Gmail address is not on the test-user list. Go back to Step C, items 7 to 9, then sign in again.

**"Token has been expired or revoked"**
Your 7-day test sign-in ran out. Click "Sign in with Google" again. If it keeps failing, delete `token.json` and sign in once more.

**"Access blocked: Inbox Cleanup has not completed the Google verification process"**
This appears when the app's publishing status is "In production" and the sign-in cannot proceed. Switch it back to "Testing" under "Audience" and make sure you are listed as a test user.

**The scan keeps saying "rate limited"**
Gmail is telling the app to slow down. The app waits and retries by itself; the scan still finishes, just more slowly.

**Nothing happens after "Sign in with Google"**
Look at the terminal window. The sign-in link is printed there; copy it into your browser if the window did not open by itself.
