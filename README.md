# Canvas to Telegram quiz notifier

The GitHub Actions workflow polls Canvas every five minutes and sends a Telegram message when a quiz becomes visible to the Canvas API user. Notification state is kept in `canvas_quiz_state.json` and committed by the workflow so scheduled runs remain idempotent.

## GitHub configuration

Add these repository secrets under **Settings → Secrets and variables → Actions**:

- `CANVAS_BASE_URL` — your Canvas instance URL, such as `https://school.instructure.com`
- `CANVAS_API_TOKEN` — a Canvas API token that can read the relevant courses and quizzes
- `TELEGRAM_BOT_TOKEN` — the token from BotFather
- `TELEGRAM_CHAT_ID` — the destination chat or channel ID

Optionally add the repository variable `CANVAS_COURSE_IDS` as a comma-separated list of course IDs. If it is omitted, the workflow discovers the active courses available to the Canvas token.

The workflow can also be run manually with **Actions → Canvas quiz notifier → Run workflow**. Credentials must be configured before doing so.

No credentials belong in this repository. The state file contains only quiz metadata and notification timestamps.
