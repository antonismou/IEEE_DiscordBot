# IEEE Student Branch TUC Discord Bot: Design

Date: 2026-10-04

## Purpose

A Discord bot for the IEEE Student Branch of the Technical University of Crete (TUC) server.

1. Verify that a member owns a `@tuc.gr` academic email, and store the verified email and real name.
2. Post IEEE Spectrum and IEEE Xplore RSS items into announcement channels.
3. Let officers manually verify people who have no TUC email (professors, alumni, guests).

## Decisions made

| Topic | Decision |
|---|---|
| Language / libs | Python, `discord.py`, `feedparser`, `smtplib`, `sqlite3` |
| News source | RSS feeds, not the inbox (the bot never accesses a mailbox) |
| Email sender | Dedicated branch Gmail account via SMTP, app password |
| Hosting | Old PC at home turned into a server; Docker Compose or systemd |
| Allowed domains | `tuc.gr` and any `*.tuc.gr` subdomain, matched strictly |
| Real name | Typed by the member in the form; never guessed from the email |
| Sections | Out of scope; officers manage section roles by hand |

## Components

### 1. Verification
- A `#verify` message with a **Verify** button opens a modal asking for full name and `@tuc.gr` email, with a consent line about storage.
- The domain check is strict: after lowercasing, the part after the last `@` must equal `tuc.gr` or end with `.tuc.gr`. `evil-tuc.gr` and `tuc.gr.fake.com` are rejected.
- The bot emails a 6-digit code (cryptographically random). The code expires after 10 minutes and allows 5 attempts. Only a hash of the code is stored.
- An **Enter code** button opens a second modal. On success the member gets the `Verified` role.
- An email already linked to another Discord account is refused.
- Rate limits: a per-user resend cooldown and a global hourly send cap to protect the Gmail account.

### 2. Manual verification (officers)
- `/verify-manual user:<member> name:<text> note:<text>` gives the `Verified` role without an email check.
- The member is stored with `method = manual`, the verifying officer's ID, and the note (reason).
- `/unverify user:<member>` removes the role. A member's stored data stays until they use `/forget-me` or an officer runs `/member delete`.
- Only members with the configured officer role can run these commands.

### 3. Data (SQLite, one file)
- `members`: `discord_id` (PK), `email` (nullable for manual entries, unique when set), `full_name`, `method` (`email` or `manual`), `verified_by` (nullable), `note`, `consent_at`, `verified_at`.
- `pending_codes`: `discord_id`, `email`, `code_hash`, `expires_at`, `attempts`.
- `seen_items`: `feed_id`, `item_id`, `posted_at`.
- Officer commands: `/member lookup`, `/member export` (CSV), `/member delete`. Members can run `/forget-me`.
- Nightly backup of the database file, keeping the last 7 copies.

### 4. RSS announcements
- Feeds are defined in config as a list of entries: `{id, url, channel_id}`. The list is curated, not "all of Xplore" (about 200+ publications would flood Discord).
- Initial feeds:
  - IEEE Spectrum, posted to `#ieee-spectrum`.
  - IEEE Xplore, a few publications per topic, one channel per topic: `#ai-ml`, `#power-energy`, `#robotics`. Feed URL format: `https://ieeexplore.ieee.org/rss/TOC<punumber>.XML`. The tested publications are listed in "Initial feed list" below.
- Item content differs by source. Spectrum items have HTML in the description: the bot strips tags, truncates to about 300 characters, and takes the first `<img>` as the embed image. Xplore items have only title, link and date; the description is the literal text `null`, so the bot ignores it and the embed shows the title, the journal name and the date (no abstract).
- Requests send a descriptive `User-Agent` and a 20 second timeout. Feeds return up to 50 items, so the first-run and per-poll cap rules below are required.
- Officers can run `/feed add`, `/feed remove` and `/feed list` to manage feeds at runtime. Feeds added this way are stored in the database and merged with the config list.
- One background task polls each feed every 30 minutes. Each new item is posted as an embed (title, short summary, image if present, link) and recorded in `seen_items` so nothing posts twice.
- Per-poll cap: at most 10 new items per feed per poll (config value `max_items_per_poll`, default 10). Any extra items are marked as seen and summarized in one line ("+N more new items from <feed title> were not shown"; the feed's XML URL is not useful to readers, so no link is included), so an issue drop doesn't flood the channel.
- Batching: the new items from one feed in one poll are sent together in as few messages as possible. A message holds up to 10 embeds, and a batch is split earlier if the embeds' total text approaches Discord's 6000-character per-message limit. Each item is its own embed with at most one image (Spectrum only). Both limits are from memory and must be verified against the Discord docs during implementation. `discord.py` handles channel rate limits by waiting, so bursts do not fail.
- On the first run of a feed, existing items are marked as seen without posting.
- A failing feed is logged and retried on the next poll; it does not affect other feeds.

#### Initial feed list (all fetched successfully with curl on 2026-10-04)

| Channel | Publication | URL |
|---|---|---|
| `#ieee-spectrum` | IEEE Spectrum | `https://spectrum.ieee.org/feeds/feed.rss` |
| `#ai-ml` | Trans. Pattern Analysis and Machine Intelligence | `https://ieeexplore.ieee.org/rss/TOC34.XML` |
| `#ai-ml` | Trans. Neural Networks and Learning Systems | `https://ieeexplore.ieee.org/rss/TOC5962385.XML` |
| `#ai-ml` | Trans. Artificial Intelligence | `https://ieeexplore.ieee.org/rss/TOC9078688.XML` |
| `#ai-ml` | Trans. Cybernetics | `https://ieeexplore.ieee.org/rss/TOC6221036.XML` |
| `#power-energy` | Trans. Power Systems | `https://ieeexplore.ieee.org/rss/TOC59.XML` |
| `#power-energy` | Trans. Power Electronics | `https://ieeexplore.ieee.org/rss/TOC63.XML` |
| `#power-energy` | Trans. Smart Grid | `https://ieeexplore.ieee.org/rss/TOC5165411.XML` |
| `#power-energy` | Trans. Sustainable Energy | `https://ieeexplore.ieee.org/rss/TOC5165391.XML` |
| `#robotics` | Trans. Robotics | `https://ieeexplore.ieee.org/rss/TOC8860.XML` |
| `#robotics` | Robotics and Automation Letters | `https://ieeexplore.ieee.org/rss/TOC7083369.XML` |
| `#robotics` | Trans. Automation Science and Engineering | `https://ieeexplore.ieee.org/rss/TOC8856.XML` |
| `#robotics` | Trans. Mechatronics | `https://ieeexplore.ieee.org/rss/TOC3516.XML` |
| `#robotics` | Robotics and Automation Magazine | `https://ieeexplore.ieee.org/rss/TOC100.XML` |

IEEE Access (`TOC6287639`) also works but is very high volume across all fields, so it is not in the initial list. Spectrum's older `/rss/fulltext` URL redirects (301); use the `/feeds/feed.rss` URL above.

### 5. Config and deployment
- Secrets in `.env` (never committed): Discord token, Gmail address, Gmail app password.
- Non-secret settings in a config file: guild ID, role IDs (`Verified`, officer), channel IDs, feed list, poll interval.
- Runs under Docker Compose or systemd with automatic restart. Database and backups live on a mounted volume.

## Error handling
- SMTP failure: tell the member to try again later; do not store a pending code.
- Wrong or expired code: clear message and remaining attempts; after 5 failures the code is invalidated.
- Missing permissions (the bot role is below `Verified`): log a clear error and tell officers.
- Feed or network errors: log and retry on the next poll.

## Testing
- Unit tests: per-poll cap behavior, embed batching (10-embed and size splits), domain validation (accepts `tuc.gr` and `*.tuc.gr`, rejects look-alikes), code expiry and attempt limits, the duplicate-email check, RSS de-duplication and first-run behavior, and the officer permission check.
- Discord and SMTP calls are mocked. A manual end-to-end run on a test server is part of the plan.

## Privacy
Emails and names are personal data under GDPR. The consent line in the verify form, `/forget-me`, officer-only access to exports, and a database file readable only by the bot's user are required, not optional.

## Out of scope (version 1)
Section roles, reading newsletters from the inbox, guessing names from emails, a welcome/rules message, `/stats`, automatic cleanup when members leave, events and reminders, annual re-verification.

## Open items
- **Xplore reachability from the server.** The feeds load with plain curl from the dev PC, but a generic fetch tool got HTTP 418 from Xplore. IEEE may rate-limit or block some clients or IP ranges. The implementation plan must include a test of the feeds from the home server, and the bot should log HTTP status codes so a block is visible.
- Whether to add the deferred items above (welcome message, `/stats`, auto cleanup on leave) is still open; they are excluded until you say otherwise.
