# IEEE Student Branch TUC Discord Bot

Verifies `@tuc.gr` members by emailed code, lets officers verify others manually, and posts curated IEEE Spectrum
and IEEE Xplore RSS items.

## Setup

1. **Discord application:** create a bot at https://discord.com/developers/applications, copy its token, and invite it
   with the `bot` and `applications.commands` scopes and the permissions *Manage Roles*, *View Channels*,
   *Send Messages* and *Embed Links*. In the server, drag the bot's role **above** the `Verified` role.
2. **Gmail:** use a dedicated branch Gmail account. Turn on 2-Step Verification, then create an *App password*
   (Google Account -> Security -> App passwords).
3. **Secrets:** `cp .env.example .env` and fill `DISCORD_TOKEN`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`.
4. **Config:** `cp config.example.toml config.toml` and fill the guild, role and channel IDs
   (enable Developer Mode in Discord, then right-click -> Copy ID). Set `officer_log_channel_id` to a private
   officers-only channel so the bot can tell you when it cannot assign the Verified role.
5. **Check the feeds from the server:** `python -m scripts.check_feeds` (all lines should say `OK`).
6. **Run:** `mkdir -p data && docker compose up -d --build`, then watch `docker compose logs -f`.
7. In `#verify` run `/setup-verify` once to post the panel.

## Commands

| Command | Who | What |
|---|---|---|
| `/setup-verify` | officers | post the verification panel |
| `/verify-manual user name note` | officers | verify someone without a TUC email |
| `/unverify user` | officers | remove the Verified role (data kept) |
| `/member lookup / export / delete` | officers | inspect, export CSV, delete stored data |
| `/feed list / add / remove` | officers | manage feeds without restarting |
| `/forget-me` | everyone | delete your own stored data |

## Data

Emails and names are personal data (GDPR). They are stored in `data/bot.sqlite3` (mode 0600), backed up nightly to
`data/backups/` (last 7 kept). Only officers can read or export them. `/forget-me` and `/member delete` remove a
person from the database immediately and from the backups within 7 days. Rate-limit records keep a hash of the
recipient address for at most 24 hours.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```
