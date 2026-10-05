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
4. **Config:** `cp config.example.toml config.toml`. Nothing in it needs editing: the server, the roles and the
   channels are all set from Discord (steps 7-9).
5. **Run:** `docker compose up -d --build`, then watch `docker compose logs -f`.
   `config.toml` must exist **before** the first `up`; otherwise Docker creates an empty folder with that name
   (the bot tells you so and how to fix it). The database lives in a Docker volume, so there is no data folder
   to create and no file owner to set.
6. **Check the feeds from the server's own network:**
   `docker compose run --rm bot python -m scripts.check_feeds /app/config.toml` (all lines should say `OK`;
   IEEE may block some addresses, which is why this runs on the server).
7. As the server owner or an administrator, run `/setup roles verified:@Role officer:@Role`. Pick any two
   different roles. This also ties the bot to that server. If the bot's role is not above the Verified role (or
   lacks Manage Roles), the reply warns you. `/setup status` shows what is still missing.
8. In `#verify` run `/setup-verify` once to post the panel.
9. Choose where each feed topic posts: `/channel set topic:ai-ml channel:#ai-ml`, and the same for `power-energy`,
   `robotics` and `ieee-spectrum`. Also set `officer-log` to a private officers-only channel: the bot tells you
   there when it cannot assign the Verified role. `/channel list` shows what is set. Feeds whose topic has no
   channel yet stay paused (the log says so).

10. **Branch roles:** create four roles (Main Branch, CS, IAS, Quantum) and run
    `/setup branches main:@.. cs:@.. ias:@.. quantum:@..`. Drag the bot's role above them and give it
    **Manage Roles** and **Manage Nicknames**. After verifying, members pick one or more of them.
11. **Channel visibility (done by hand in Discord):** `@everyone` sees only `#welcome`, `#rules` and `#verify`;
    the **Verified** role sees everything else. Make a `#roles` channel visible to Verified only and run
    `/setup-roles` there: members press **Choose my roles** to change their branches later, no commands needed.

On verification the bot renames the member to the full name they gave (cut to Discord's 32 characters). It cannot
rename the server owner or anyone above its role; officers are told in `officer-log` when that happens.

Administrators can always use the officer commands, even before the officer role exists.

## Commands

| Command | Who | What |
|---|---|---|
| `/setup roles / status` | administrators | choose the Verified and Officer roles; see what is missing |
| `/setup branches` | administrators | choose the four branch roles members can pick |
| `/setup branch-description` | administrators | set the short text (max 100 characters) shown under each branch in the picker; leave `text` empty to remove it |
| `/setup-roles` | officers | post the "Choose my roles" panel in the current channel |
| `/setup-verify` | officers | post the verification panel |
| `/verify-manual user name note` | officers | verify someone without a TUC email |
| `/unverify user` | officers | remove the Verified role (data kept) |
| `/member lookup / export / delete` | officers | inspect, export CSV, delete stored data |
| `/channel set / clear / list` | officers | choose the channel each feed topic (and the officer log) posts to |
| `/feed list / add / remove` | officers | manage feeds without restarting |
| `/forget-me` | everyone | delete your own stored data |

## Data

Emails and names are personal data (GDPR). They are stored in the Docker volume `bot-data` (`bot.sqlite3`, mode 0600), backed up nightly inside it to
`backups/` (last 7 kept). Only officers can read or export them. `/forget-me` and `/member delete` remove a
person from the database immediately and from the backups within 7 days. Rate-limit records keep a hash of the
recipient address for at most 24 hours.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

## Data and backups

The data lives in the Docker volume `bot-data`, not in the project folder. Copy the backups out with
`docker compose cp bot:/app/data/backups ./backups-copy`.

`docker compose down` and updates keep the data. **`docker compose down -v` deletes the volume, and with it all
stored members**, so never add `-v` unless you mean it.
