# IEEE TUC Discord Bot Implementation Plan


**Goal:** A Discord bot that verifies `@tuc.gr` members by emailed code (storing email and name), lets officers verify people manually, and posts curated IEEE Spectrum and IEEE Xplore RSS items into topic channels.

**Architecture:** One Python process. A synchronous core (SQLite repos, `VerificationService`, feed parsing, batching, `select_new`) holds all logic and is unit-tested. A thin layer of `discord.py` cogs (verify, officer, feeds, maintenance) wires the core to Discord. Blocking work (SMTP, HTTP) runs via `asyncio.to_thread`; all SQLite access stays on the event-loop thread.

**Tech Stack:** Python 3.11+ (`tomllib`), `discord.py` 2.x, `feedparser`, `requests`, `python-dotenv`, `sqlite3`, `smtplib`, `pytest`, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-04-ieee-discord-bot-design.md`

## Global Constraints

- Python 3.11 or newer (the config loader uses `tomllib`).
- Allowed email domains: `tuc.gr` and any `*.tuc.gr` subdomain, matched strictly. `evil-tuc.gr` and `tuc.gr.fake.com` are rejected.
- Verification code: 6 digits from `secrets`, expires after 10 minutes (600 s), 5 attempts, only a hash is stored.
- Resend cooldown per user: 60 s. Global send cap: 40 verification emails per rolling hour.
- The real name is typed by the member, 2-100 characters, never guessed from the email.
- Secrets (`DISCORD_TOKEN`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`) live only in `.env`, which is never committed.
- Database file and backups are mode `0600`; the data directory is `0700`.
- Feeds are polled every 30 minutes (`poll_interval_minutes`), at most 10 new items per feed per poll (`max_items_per_poll`).
- Feed requests send `User-Agent: IEEE-TUC-DiscordBot/1.0 (student branch RSS reader)` with a 20 second timeout and a 5 MB size cap.
- On a feed's first run, existing items are marked as seen and nothing is posted.
- A Discord message holds at most 10 embeds; batches are also split at 5500 characters of embed text (Discord's limit is 6000 per message). Both limits are from memory: Task 9 verifies them against the discord.py docs.
- An Xplore item's description is the literal text `null` and must be ignored.
- Nightly SQLite backup, keeping the last 7.
- Section roles, newsletter-by-inbox, name guessing, welcome message, `/stats` and auto-cleanup on leave are out of scope.

## Review Focus

The spec implies these inputs but does not spell them out. Each has a test in the task that owns the code.

1. **Email header injection and look-alike domains** (`a@tuc.gr\nBcc: x@y.com`, Cyrillic `с` in `tuc.gr`, Kelvin sign): must be rejected, never reach SMTP. Task 2 and Task 5.
2. **CSV formula injection in `/member export`** (a name such as `=HYPERLINK(...)`): must be neutralised so Excel does not run it. Task 3.
3. **A feed URL returning HTTP 200 with an HTML or text error page**: must raise a feed error, must not be treated as "no new items", and must not initialise the feed (so the first real response still counts as first run). Task 8 and Task 9.
4. **Discord send failing midway through a poll**: items already posted stay seen, the rest stay unseen and are retried next poll, and nothing is posted twice. Task 9.
5. **Replayed or malformed verification codes** (reusing a code after success, `"123 456"` with a space, letters, wrong code on the last attempt): must behave sensibly and never verify twice. Task 4.

---

## File Structure

```
IEEE_DiscordBot/
  .env.example              # secret names only
  .gitignore
  config.example.toml       # IDs, channels, the 14 tested feeds
  requirements.txt
  requirements-dev.txt
  pytest.ini
  Dockerfile
  docker-compose.yml
  README.md
  bot/
    __init__.py
    config.py               # Settings, FeedConfig, load_settings
    validation.py           # is_tuc_email, normalize_email, clean_text
    db.py                   # connect(), schema, time helpers
    members.py              # Member, MemberRepo, members_to_csv
    verification.py         # VerificationService, VerificationError
    mailer.py               # MailError, build_message, make_gmail_sender
    roles.py                # set_verified_role
    checks.py               # is_officer, officer_only, handle_app_command_error
    backup.py               # backup_database
    app.py                  # AppContext, build_app
    main.py                 # IEEEBot, entry point
    feeds/
      __init__.py
      parse.py              # FeedItem, parse_feed, fetch_feed, FeedError
      batch.py              # item_to_embed, batch_embeds
      store.py              # SeenStore, CustomFeedRepo
      service.py            # select_new, process_feed
    cogs/
      __init__.py
      verify.py             # buttons, modals, /setup-verify
      officer.py            # /verify-manual, /unverify, /member ..., /forget-me
      feeds.py              # poll loop, /feed ...
      maintenance.py        # nightly backup loop
  scripts/
    check_feeds.py          # run on the server to test feed reachability
  tests/
    conftest.py
    test_*.py
```

---

### Task 1: Project scaffold and config loader

**Files:**
- Create: `.gitignore`, `.env.example`, `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `bot/__init__.py`, `bot/feeds/__init__.py`, `bot/cogs/__init__.py`, `bot/config.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `bot.config.ConfigError`, `FeedConfig(id: str, title: str, url: str, channel_id: int)`, `Settings` (fields below), `load_settings(config_path: Path, env: Mapping[str, str] | None = None) -> Settings`.

`Settings` fields: `discord_token: str`, `gmail_address: str`, `gmail_app_password: str`, `guild_id: int`, `verified_role_id: int`, `officer_role_id: int`, `db_path: Path`, `backup_dir: Path`, `poll_interval_minutes: int`, `max_items_per_poll: int`, `feeds: tuple[FeedConfig, ...]`.

- [ ] **Step 1: Initialise the repository and tooling**

```bash
cd /home/antonis/Documents/GitHub/IEEE_DiscordBot
git init
python3 -m venv .venv
mkdir -p bot/feeds bot/cogs tests scripts
touch bot/__init__.py bot/feeds/__init__.py bot/cogs/__init__.py
```

`.gitignore`:
```
.venv/
__pycache__/
*.pyc
.env
config.toml
data/
.pytest_cache/
```

`.env.example`:
```
DISCORD_TOKEN=
GMAIL_ADDRESS=
GMAIL_APP_PASSWORD=
```

`requirements.txt`:
```
discord.py>=2.4,<3
feedparser>=6.0,<7
requests>=2.31,<3
python-dotenv>=1.0,<2
```

`requirements-dev.txt`:
```
-r requirements.txt
pytest>=8
```

`pytest.ini`:
```
[pytest]
testpaths = tests
pythonpath = .
```

Then: `.venv/bin/pip install -r requirements-dev.txt`

- [ ] **Step 2: Write the failing test**

`tests/test_config.py`:
```python
from pathlib import Path

import pytest

from bot.config import ConfigError, load_settings

ENV = {"DISCORD_TOKEN": "t", "GMAIL_ADDRESS": "a@gmail.com", "GMAIL_APP_PASSWORD": "p"}

GOOD = """
guild_id = 1
verified_role_id = 2
officer_role_id = 3

[channels]
ai-ml = 10

[[feeds]]
id = "tpami"
title = "Pattern Analysis and Machine Intelligence"
url = "https://ieeexplore.ieee.org/rss/TOC34.XML"
channel = "ai-ml"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_values_and_defaults(tmp_path):
    s = load_settings(write(tmp_path, GOOD), ENV)
    assert (s.guild_id, s.verified_role_id, s.officer_role_id) == (1, 2, 3)
    assert s.discord_token == "t" and s.gmail_address == "a@gmail.com"
    assert s.poll_interval_minutes == 30 and s.max_items_per_poll == 10
    assert s.db_path == Path("data/bot.sqlite3")
    assert s.backup_dir == Path("data/backups")
    (feed,) = s.feeds
    assert (feed.id, feed.channel_id) == ("tpami", 10)


def test_missing_env_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="GMAIL_APP_PASSWORD"):
        load_settings(write(tmp_path, GOOD), {"DISCORD_TOKEN": "t", "GMAIL_ADDRESS": "a"})


def test_unknown_channel_name(tmp_path):
    with pytest.raises(ConfigError, match="nope"):
        load_settings(write(tmp_path, GOOD.replace('channel = "ai-ml"', 'channel = "nope"')), ENV)


def test_duplicate_feed_id(tmp_path):
    text = GOOD + '\n[[feeds]]\nid = "tpami"\ntitle = "x"\nurl = "https://x"\nchannel = "ai-ml"\n'
    with pytest.raises(ConfigError, match="Duplicate feed id"):
        load_settings(write(tmp_path, text), ENV)


def test_missing_file_and_missing_key(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.toml", ENV)
    with pytest.raises(ConfigError, match="guild_id"):
        load_settings(write(tmp_path, GOOD.replace("guild_id = 1", "")), ENV)
```

- [ ] **Step 3: Run it to see it fail**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL (`ModuleNotFoundError: bot.config`).

- [ ] **Step 4: Implement**

`bot/config.py`:
```python
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

REQUIRED_ENV = ("DISCORD_TOKEN", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class FeedConfig:
    id: str
    title: str
    url: str
    channel_id: int


@dataclass(frozen=True)
class Settings:
    discord_token: str
    gmail_address: str
    gmail_app_password: str
    guild_id: int
    verified_role_id: int
    officer_role_id: int
    db_path: Path
    backup_dir: Path
    poll_interval_minutes: int
    max_items_per_poll: int
    feeds: tuple[FeedConfig, ...]


def load_settings(config_path: Path, env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED_ENV if not env.get(name)]
    if missing:
        raise ConfigError(f"Missing environment variables: {', '.join(missing)}")
    try:
        raw = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {config_path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid config file: {exc}") from None

    channels = raw.get("channels", {})
    feeds: list[FeedConfig] = []
    seen_ids: set[str] = set()
    for entry in raw.get("feeds", []):
        try:
            feed_id, title, url, channel = entry["id"], entry["title"], entry["url"], entry["channel"]
        except KeyError as exc:
            raise ConfigError(f"Feed entry is missing key {exc}: {entry}") from None
        if channel not in channels:
            raise ConfigError(f"Feed '{feed_id}' uses unknown channel '{channel}'")
        if feed_id in seen_ids:
            raise ConfigError(f"Duplicate feed id '{feed_id}'")
        seen_ids.add(feed_id)
        feeds.append(FeedConfig(feed_id, title, url, int(channels[channel])))

    try:
        return Settings(
            discord_token=env["DISCORD_TOKEN"],
            gmail_address=env["GMAIL_ADDRESS"],
            gmail_app_password=env["GMAIL_APP_PASSWORD"],
            guild_id=int(raw["guild_id"]),
            verified_role_id=int(raw["verified_role_id"]),
            officer_role_id=int(raw["officer_role_id"]),
            db_path=Path(raw.get("db_path", "data/bot.sqlite3")),
            backup_dir=Path(raw.get("backup_dir", "data/backups")),
            poll_interval_minutes=int(raw.get("poll_interval_minutes", 30)),
            max_items_per_poll=int(raw.get("max_items_per_poll", 10)),
            feeds=tuple(feeds),
        )
    except KeyError as exc:
        raise ConfigError(f"Missing config key: {exc}") from None
```

- [ ] **Step 5: Run it to see it pass**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: project scaffold, spec, plan and config loader"
```

---

### Task 2: Email and text validation

**Files:**
- Create: `bot/validation.py`, `tests/test_validation.py`

**Interfaces:**
- Produces: `normalize_email(email: str) -> str`, `is_tuc_email(email: str) -> bool`, `clean_text(raw: str, *, min_len: int, max_len: int, label: str) -> str` (raises `ValueError` with a message safe to show to a user).

- [ ] **Step 1: Write the failing test**

`tests/test_validation.py`:
```python
import pytest

from bot.validation import clean_text, is_tuc_email, normalize_email


@pytest.mark.parametrize("email", [
    "student@tuc.gr",
    "Student.Name@TUC.GR",
    "a1@isc.tuc.gr",
    "x@ece.tuc.gr",
    "  padded@tuc.gr  ",
    "first.last+tag@tuc.gr",
])
def test_accepts_tuc_addresses(email):
    assert is_tuc_email(email)


@pytest.mark.parametrize("email", [
    "x@gmail.com",
    "x@evil-tuc.gr",
    "x@tuc.gr.fake.com",
    "x@.tuc.gr",
    "x@tuc.grx",
    "x@@tuc.gr",
    "@tuc.gr",
    "x@tuc",
    "",
    "a@tuc.gr\nBcc: victim@example.com",   # header injection
    "a b@tuc.gr",
    "x@tuс.gr",                       # Cyrillic 'с' look-alike
    "K@tuc.gr",                       # Kelvin sign lowercases to ASCII 'k'
])
def test_rejects_everything_else(email):
    assert not is_tuc_email(email)


def test_normalize_email_lowercases_and_strips():
    assert normalize_email("  A.B@TUC.gr ") == "a.b@tuc.gr"


def test_clean_text_collapses_whitespace_and_strips():
    assert clean_text("  Maria   Papadopoulou \n", min_len=2, max_len=100, label="Name") == "Maria Papadopoulou"


def test_clean_text_accepts_greek():
    assert clean_text("Αντώνης Μουτσάν", min_len=2, max_len=100, label="Name") == "Αντώνης Μουτσάν"


@pytest.mark.parametrize("raw", ["", " ", "A", "x" * 101, "bad\u200bname", "bell\x07"])
def test_clean_text_rejects_bad_input(raw):
    with pytest.raises(ValueError):
        clean_text(raw, min_len=2, max_len=100, label="Name")


def test_error_message_names_the_field():
    with pytest.raises(ValueError, match="Name"):
        clean_text("A", min_len=2, max_len=100, label="Name")
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/pytest tests/test_validation.py -v`
Expected: FAIL (`ModuleNotFoundError: bot.validation`).

- [ ] **Step 3: Implement**

`bot/validation.py`:
```python
from __future__ import annotations

import re
import unicodedata

_LOCAL = re.compile(r"[a-z0-9._%+\-]+")
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)*tuc\.gr")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def is_tuc_email(email: str) -> bool:
    """True for local@tuc.gr and local@<labels>.tuc.gr, ASCII only."""
    raw = email.strip()
    if not raw.isascii():  # checked before lower(): e.g. Kelvin sign lowercases to ASCII 'k'
        return False
    candidate = raw.lower()
    if candidate.count("@") != 1:
        return False
    local, domain = candidate.split("@")
    return bool(_LOCAL.fullmatch(local)) and bool(_DOMAIN.fullmatch(domain))


def clean_text(raw: str, *, min_len: int, max_len: int, label: str) -> str:
    text = " ".join(raw.split())
    if any(unicodedata.category(ch).startswith("C") for ch in text):
        raise ValueError(f"{label} contains invalid characters.")
    if len(text) < min_len:
        raise ValueError(f"{label} must be at least {min_len} characters.")
    if len(text) > max_len:
        raise ValueError(f"{label} must be at most {max_len} characters.")
    return text
```

- [ ] **Step 4: Run it to see it pass**

Run: `.venv/bin/pytest tests/test_validation.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add bot/validation.py tests/test_validation.py
git commit -m "feat: strict tuc.gr email and text validation"
```

---

### Task 3: Database and member repository

**Files:**
- Create: `bot/db.py`, `bot/members.py`, `tests/conftest.py`, `tests/test_db.py`, `tests/test_members.py`

**Interfaces:**
- Produces from `bot.db`: `connect(path) -> sqlite3.Connection` (row factory `sqlite3.Row`, schema created, file mode `0600`), `utcnow() -> datetime` (timezone-aware UTC), `to_iso(dt: datetime) -> str`, `from_iso(s: str) -> datetime`.
- Produces from `bot.members`: `Member` dataclass (`discord_id: int, email: str | None, full_name: str, method: str, verified_by: int | None, note: str | None, consent_at: str | None, verified_at: str`), `EmailAlreadyUsed`, `MemberRepo(conn)` with `get(discord_id) -> Member | None`, `get_by_email(email) -> Member | None`, `add_email_member(*, discord_id, email, full_name, consent_at: datetime, verified_at: datetime) -> Member`, `add_manual_member(*, discord_id, full_name, verified_by: int, note: str, verified_at: datetime) -> Member`, `delete(discord_id) -> bool`, `all() -> list[Member]`; `members_to_csv(members: list[Member]) -> str`.
- Test fixtures in `tests/conftest.py`: `conn` (a fresh database in `tmp_path`) and `members` (`MemberRepo(conn)`).

- [ ] **Step 1: Write the failing tests**

`tests/conftest.py`:
```python
import pytest

from bot.db import connect
from bot.members import MemberRepo


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "data" / "bot.sqlite3")
    yield connection
    connection.close()


@pytest.fixture
def members(conn):
    return MemberRepo(conn)
```

`tests/test_db.py`:
```python
import os
import stat
from datetime import datetime, timezone

from bot.db import connect, from_iso, to_iso, utcnow


def test_database_and_wal_files_are_private(tmp_path):
    path = tmp_path / "data" / "bot.sqlite3"
    conn = connect(path)
    conn.execute("INSERT INTO send_log (sent_at) VALUES ('x')")
    conn.commit()
    for suffix in ("", "-wal"):
        file = path.parent / (path.name + suffix)
        if file.exists():
            assert stat.S_IMODE(os.stat(file).st_mode) == 0o600, file
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    conn.close()


def test_schema_is_idempotent(tmp_path):
    path = tmp_path / "bot.sqlite3"
    connect(path).close()
    connect(path).close()


def test_time_helpers_round_trip():
    now = utcnow()
    assert now.tzinfo is not None
    assert from_iso(to_iso(now)) == now.replace(microsecond=0)
    assert to_iso(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "2026-01-01T00:00:00+00:00"
```

`tests/test_members.py`:
```python
import csv
import io
from datetime import datetime, timezone

import pytest

from bot.members import EmailAlreadyUsed, members_to_csv

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def add_email(members, discord_id=1, email="a@tuc.gr", name="Alice"):
    return members.add_email_member(
        discord_id=discord_id, email=email, full_name=name, consent_at=NOW, verified_at=NOW
    )


def test_add_and_get(members):
    added = add_email(members)
    assert members.get(1) == added
    assert added.method == "email" and added.email == "a@tuc.gr"
    assert members.get_by_email("a@tuc.gr").discord_id == 1
    assert members.get(99) is None and members.get_by_email("zz@tuc.gr") is None


def test_same_user_can_update_their_record(members):
    add_email(members, name="Alice")
    add_email(members, email="new@tuc.gr", name="Alice B")
    assert members.get(1).full_name == "Alice B"
    assert members.get_by_email("a@tuc.gr") is None


def test_email_cannot_belong_to_two_accounts(members):
    add_email(members, discord_id=1)
    with pytest.raises(EmailAlreadyUsed):
        add_email(members, discord_id=2)
    assert members.get(2) is None


def test_manual_members_have_no_email_and_can_be_many(members):
    m = members.add_manual_member(discord_id=5, full_name="Prof X", verified_by=100, note="Professor", verified_at=NOW)
    members.add_manual_member(discord_id=6, full_name="Alum Y", verified_by=100, note="Alumni", verified_at=NOW)
    assert m.method == "manual" and m.email is None and m.verified_by == 100 and m.note == "Professor"
    assert len(members.all()) == 2


def test_delete(members):
    add_email(members)
    assert members.delete(1) is True
    assert members.delete(1) is False
    assert members.get(1) is None


def test_csv_has_header_and_rows(members):
    add_email(members)
    rows = list(csv.reader(io.StringIO(members_to_csv(members.all()))))
    assert rows[0] == ["discord_id", "email", "full_name", "method", "verified_by", "note", "consent_at", "verified_at"]
    assert rows[1][:4] == ["1", "a@tuc.gr", "Alice", "email"]


@pytest.mark.parametrize("name", ["=HYPERLINK(\"http://x\",\"y\")", "+1+1", "-2+3", "@SUM(A1)"])
def test_csv_neutralises_formulas(members, name):
    members.add_manual_member(discord_id=7, full_name=name, verified_by=1, note="n", verified_at=NOW)
    rows = list(csv.reader(io.StringIO(members_to_csv(members.all()))))
    assert rows[1][2] == "'" + name
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_db.py tests/test_members.py -v`
Expected: FAIL (`ModuleNotFoundError: bot.db`).

- [ ] **Step 3: Implement `bot/db.py`**

```python
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    discord_id  INTEGER PRIMARY KEY,
    email       TEXT UNIQUE,
    full_name   TEXT NOT NULL,
    method      TEXT NOT NULL CHECK (method IN ('email', 'manual')),
    verified_by INTEGER,
    note        TEXT,
    consent_at  TEXT,
    verified_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_codes (
    discord_id INTEGER PRIMARY KEY,
    email      TEXT NOT NULL,
    full_name  TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS send_log (
    sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_items (
    feed_id   TEXT NOT NULL,
    item_id   TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    PRIMARY KEY (feed_id, item_id)
);
CREATE TABLE IF NOT EXISTS feed_state (
    feed_id        TEXT PRIMARY KEY,
    initialized_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custom_feeds (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    url        TEXT NOT NULL,
    channel_id INTEGER NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Create the file as 0600 first so the -wal/-shm files SQLite derives from it are private too.
    os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
```

- [ ] **Step 4: Implement `bot/members.py`**

```python
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from bot.db import to_iso


class EmailAlreadyUsed(Exception):
    pass


@dataclass(frozen=True)
class Member:
    discord_id: int
    email: str | None
    full_name: str
    method: str
    verified_by: int | None
    note: str | None
    consent_at: str | None
    verified_at: str


_COLUMNS = "discord_id, email, full_name, method, verified_by, note, consent_at, verified_at"


def _member(row: sqlite3.Row) -> Member:
    return Member(**{key: row[key] for key in row.keys()})


class MemberRepo:
    def __init__(self, conn: sqlite3.Connection):
        self._c = conn

    def get(self, discord_id: int) -> Member | None:
        row = self._c.execute(f"SELECT {_COLUMNS} FROM members WHERE discord_id = ?", (discord_id,)).fetchone()
        return _member(row) if row else None

    def get_by_email(self, email: str) -> Member | None:
        row = self._c.execute(f"SELECT {_COLUMNS} FROM members WHERE email = ?", (email,)).fetchone()
        return _member(row) if row else None

    def all(self) -> list[Member]:
        rows = self._c.execute(f"SELECT {_COLUMNS} FROM members ORDER BY verified_at, discord_id").fetchall()
        return [_member(row) for row in rows]

    def add_email_member(
        self, *, discord_id: int, email: str, full_name: str, consent_at: datetime, verified_at: datetime
    ) -> Member:
        try:
            with self._c:
                self._c.execute(
                    """
                    INSERT INTO members (discord_id, email, full_name, method, verified_by, note, consent_at, verified_at)
                    VALUES (?, ?, ?, 'email', NULL, NULL, ?, ?)
                    ON CONFLICT(discord_id) DO UPDATE SET
                        email = excluded.email, full_name = excluded.full_name, method = 'email',
                        verified_by = NULL, note = NULL,
                        consent_at = excluded.consent_at, verified_at = excluded.verified_at
                    """,
                    (discord_id, email, full_name, to_iso(consent_at), to_iso(verified_at)),
                )
        except sqlite3.IntegrityError:
            raise EmailAlreadyUsed(email) from None
        return self.get(discord_id)

    def add_manual_member(
        self, *, discord_id: int, full_name: str, verified_by: int, note: str, verified_at: datetime
    ) -> Member:
        with self._c:
            self._c.execute(
                """
                INSERT INTO members (discord_id, email, full_name, method, verified_by, note, consent_at, verified_at)
                VALUES (?, NULL, ?, 'manual', ?, ?, NULL, ?)
                ON CONFLICT(discord_id) DO UPDATE SET
                    email = NULL, full_name = excluded.full_name, method = 'manual',
                    verified_by = excluded.verified_by, note = excluded.note,
                    consent_at = NULL, verified_at = excluded.verified_at
                """,
                (discord_id, full_name, verified_by, note, to_iso(verified_at)),
            )
        return self.get(discord_id)

    def delete(self, discord_id: int) -> bool:
        with self._c:
            cursor = self._c.execute("DELETE FROM members WHERE discord_id = ?", (discord_id,))
        return cursor.rowcount > 0


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def members_to_csv(members: list[Member]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["discord_id", "email", "full_name", "method", "verified_by", "note", "consent_at", "verified_at"])
    for m in members:
        writer.writerow([_csv_safe(v) for v in (
            m.discord_id, m.email, m.full_name, m.method, m.verified_by, m.note, m.consent_at, m.verified_at
        )])
    return out.getvalue()
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `.venv/bin/pytest tests/test_db.py tests/test_members.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add bot/db.py bot/members.py tests/
git commit -m "feat: private sqlite database and member repository with safe CSV export"
```

---

### Task 4: Verification service

**Files:**
- Create: `bot/verification.py`, `tests/test_verification.py`

**Interfaces:**
- Consumes: `connect`/`utcnow`/`to_iso`/`from_iso` (Task 3), `MemberRepo`, `Member`, `EmailAlreadyUsed` (Task 3), `normalize_email`, `is_tuc_email`, `clean_text` (Task 2).
- Produces: `VerificationError(Exception)` (message is safe to show to users), `PendingVerification(email: str, code: str, ttl_minutes: int)`, `VerificationService(conn, members, *, clock=utcnow, code_factory=None, code_ttl_seconds=600, max_attempts=5, resend_cooldown_seconds=60, hourly_send_cap=40)` with `begin(discord_id: int, raw_name: str, raw_email: str) -> PendingVerification`, `abort(discord_id: int) -> None`, `confirm(discord_id: int, raw_code: str) -> Member`.
- `begin` stores the pending record and writes the send log; the caller sends the email afterwards and calls `abort` if sending fails.

- [ ] **Step 1: Write the failing tests**

`tests/test_verification.py`:
```python
from datetime import datetime, timedelta, timezone

import pytest

from bot.verification import VerificationError, VerificationService


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(conn, members, clock):
    return VerificationService(conn, members, clock=clock, code_factory=lambda: "123456")


def begin(service, discord_id=1, name="Alice Smith", email="alice@tuc.gr"):
    return service.begin(discord_id, name, email)


def test_begin_returns_code_for_the_normalised_email(service):
    pending = begin(service, email=" Alice@ISC.TUC.GR ")
    assert pending.email == "alice@isc.tuc.gr"
    assert pending.code == "123456" and pending.ttl_minutes == 10


def test_plain_code_is_never_stored(service, conn):
    begin(service)
    row = conn.execute("SELECT * FROM pending_codes").fetchone()
    assert "123456" not in tuple(str(v) for v in tuple(row))


@pytest.mark.parametrize("email", ["x@gmail.com", "x@evil-tuc.gr", "x@tuc.gr.fake.com"])
def test_rejects_non_tuc_email(service, email):
    with pytest.raises(VerificationError, match="tuc.gr"):
        begin(service, email=email)


def test_rejects_bad_name(service):
    with pytest.raises(VerificationError, match="Name"):
        begin(service, name="A")


def test_rejects_already_verified_user(service):
    begin(service)
    service.confirm(1, "123456")
    with pytest.raises(VerificationError, match="already verified"):
        begin(service, email="other@tuc.gr")


def test_rejects_email_linked_to_someone_else(service, clock):
    begin(service, discord_id=1)
    service.confirm(1, "123456")
    with pytest.raises(VerificationError, match="already linked"):
        begin(service, discord_id=2)


def test_resend_cooldown(service, clock):
    begin(service)
    clock.advance(30)
    with pytest.raises(VerificationError, match="30 seconds"):
        begin(service)
    clock.advance(31)
    begin(service)


def test_hourly_cap(conn, members, clock):
    service = VerificationService(
        conn, members, clock=clock, code_factory=lambda: "123456", hourly_send_cap=3, resend_cooldown_seconds=0
    )
    for i in range(3):
        service.begin(i + 1, "Alice Smith", f"user{i}@tuc.gr")
    with pytest.raises(VerificationError, match="try again later"):
        service.begin(10, "Alice Smith", "user10@tuc.gr")
    clock.advance(3601)
    service.begin(10, "Alice Smith", "user10@tuc.gr")


def test_abort_removes_pending_so_user_can_retry_immediately(service):
    begin(service)
    service.abort(1)
    begin(service)


def test_confirm_success_creates_member_with_consent_time(service, members, clock):
    begin(service, name="Alice  Smith")
    clock.advance(120)
    member = service.confirm(1, "123456")
    assert member.full_name == "Alice Smith" and member.email == "alice@tuc.gr"
    assert member.method == "email"
    assert member.consent_at == "2026-10-04T12:00:00+00:00"
    assert member.verified_at == "2026-10-04T12:02:00+00:00"
    assert members.get(1) == member


def test_code_with_spaces_is_accepted(service):
    begin(service)
    assert service.confirm(1, " 123 456 ").discord_id == 1


def test_code_cannot_be_replayed_after_success(service):
    begin(service)
    service.confirm(1, "123456")
    with pytest.raises(VerificationError, match="Press"):
        service.confirm(1, "123456")


def test_confirm_without_begin(service):
    with pytest.raises(VerificationError, match="Press"):
        service.confirm(1, "123456")


@pytest.mark.parametrize("bad", ["000000", "abcdef", "", "12345", "1234567"])
def test_wrong_or_malformed_codes_count_as_attempts(service, bad):
    begin(service)
    with pytest.raises(VerificationError, match="4 attempt"):
        service.confirm(1, bad)


def test_fifth_wrong_attempt_invalidates_the_code(service):
    begin(service)
    for _ in range(4):
        with pytest.raises(VerificationError, match="Wrong code"):
            service.confirm(1, "000000")
    with pytest.raises(VerificationError, match="Too many"):
        service.confirm(1, "000000")
    with pytest.raises(VerificationError, match="Press"):
        service.confirm(1, "123456")  # even the right code no longer works


def test_expired_code(service, clock):
    begin(service)
    clock.advance(601)
    with pytest.raises(VerificationError, match="expired"):
        service.confirm(1, "123456")
    with pytest.raises(VerificationError, match="Press"):
        service.confirm(1, "123456")


def test_email_taken_between_begin_and_confirm(service, members, clock):
    begin(service, discord_id=1)
    begin(service, discord_id=2)           # both pending for the same email
    service.confirm(1, "123456")
    with pytest.raises(VerificationError, match="already linked"):
        service.confirm(2, "123456")
    assert members.get(2) is None
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_verification.py -v`
Expected: FAIL (`ModuleNotFoundError: bot.verification`).

- [ ] **Step 3: Implement**

`bot/verification.py`:
```python
from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from bot.db import from_iso, to_iso, utcnow
from bot.members import EmailAlreadyUsed, Member, MemberRepo
from bot.validation import clean_text, is_tuc_email, normalize_email


class VerificationError(Exception):
    """The message is safe to show to the member."""


@dataclass(frozen=True)
class PendingVerification:
    email: str
    code: str
    ttl_minutes: int


def _default_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def _hash(discord_id: int, code: str) -> str:
    return hashlib.sha256(f"{discord_id}:{code}".encode()).hexdigest()


class VerificationService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        members: MemberRepo,
        *,
        clock: Callable[[], datetime] = utcnow,
        code_factory: Callable[[], str] | None = None,
        code_ttl_seconds: int = 600,
        max_attempts: int = 5,
        resend_cooldown_seconds: int = 60,
        hourly_send_cap: int = 40,
    ):
        self._c = conn
        self._members = members
        self._clock = clock
        self._code_factory = code_factory or _default_code
        self._ttl = code_ttl_seconds
        self._max_attempts = max_attempts
        self._cooldown = resend_cooldown_seconds
        self._hourly_cap = hourly_send_cap

    def begin(self, discord_id: int, raw_name: str, raw_email: str) -> PendingVerification:
        try:
            name = clean_text(raw_name, min_len=2, max_len=100, label="Name")
        except ValueError as exc:
            raise VerificationError(str(exc)) from None
        email = normalize_email(raw_email)
        if not is_tuc_email(email):
            raise VerificationError(
                "Only @tuc.gr addresses (or a tuc.gr subdomain such as @isc.tuc.gr) are accepted."
            )
        if self._members.get(discord_id):
            raise VerificationError("You are already verified.")
        if self._members.get_by_email(email):
            raise VerificationError("That email is already linked to another Discord account. Ask an officer for help.")

        now = self._clock()
        row = self._c.execute("SELECT created_at FROM pending_codes WHERE discord_id = ?", (discord_id,)).fetchone()
        if row:
            elapsed = (now - from_iso(row["created_at"])).total_seconds()
            if elapsed < self._cooldown:
                wait = math.ceil(self._cooldown - elapsed)
                raise VerificationError(f"Please wait {wait} seconds before requesting another code.")
        sent = self._c.execute(
            "SELECT COUNT(*) FROM send_log WHERE sent_at >= ?", (to_iso(now - timedelta(hours=1)),)
        ).fetchone()[0]
        if sent >= self._hourly_cap:
            raise VerificationError("Too many verification emails were sent recently. Please try again later.")

        code = self._code_factory()
        with self._c:
            self._c.execute(
                "INSERT OR REPLACE INTO pending_codes "
                "(discord_id, email, full_name, code_hash, expires_at, attempts, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (discord_id, email, name, _hash(discord_id, code),
                 to_iso(now + timedelta(seconds=self._ttl)), to_iso(now)),
            )
            self._c.execute("INSERT INTO send_log (sent_at) VALUES (?)", (to_iso(now),))
            self._c.execute("DELETE FROM send_log WHERE sent_at < ?", (to_iso(now - timedelta(hours=24)),))
        return PendingVerification(email=email, code=code, ttl_minutes=self._ttl // 60)

    def abort(self, discord_id: int) -> None:
        self._delete_pending(discord_id)

    def confirm(self, discord_id: int, raw_code: str) -> Member:
        row = self._c.execute("SELECT * FROM pending_codes WHERE discord_id = ?", (discord_id,)).fetchone()
        if row is None:
            raise VerificationError("No verification in progress. Press **Verify** first.")
        now = self._clock()
        if now >= from_iso(row["expires_at"]):
            self._delete_pending(discord_id)
            raise VerificationError("That code has expired. Press **Verify** to get a new one.")

        code = "".join(raw_code.split())
        if not hmac.compare_digest(_hash(discord_id, code), row["code_hash"]):
            attempts = row["attempts"] + 1
            if attempts >= self._max_attempts:
                self._delete_pending(discord_id)
                raise VerificationError("Too many wrong attempts. Press **Verify** to start again.")
            with self._c:
                self._c.execute("UPDATE pending_codes SET attempts = ? WHERE discord_id = ?", (attempts, discord_id))
            left = self._max_attempts - attempts
            raise VerificationError(f"Wrong code. {left} attempt(s) left.")

        try:
            member = self._members.add_email_member(
                discord_id=discord_id,
                email=row["email"],
                full_name=row["full_name"],
                consent_at=from_iso(row["created_at"]),
                verified_at=now,
            )
        except EmailAlreadyUsed:
            self._delete_pending(discord_id)
            raise VerificationError(
                "That email is already linked to another Discord account. Ask an officer for help."
            ) from None
        self._delete_pending(discord_id)
        return member

    def _delete_pending(self, discord_id: int) -> None:
        with self._c:
            self._c.execute("DELETE FROM pending_codes WHERE discord_id = ?", (discord_id,))
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/pytest tests/test_verification.py -v`
Expected: all pass. If a `match=` fails because of Markdown asterisks in a message, adjust the regex in the test, not the user-facing text.

- [ ] **Step 5: Commit**

```bash
git add bot/verification.py tests/test_verification.py
git commit -m "feat: verification service with expiry, attempts, cooldown and send cap"
```

---

### Task 5: Gmail mailer

**Files:**
- Create: `bot/mailer.py`, `tests/test_mailer.py`

**Interfaces:**
- Produces: `MailError(Exception)`, `build_message(sender: str, to: str, code: str, ttl_minutes: int) -> EmailMessage`, `make_gmail_sender(address: str, app_password: str, *, host="smtp.gmail.com", port=465) -> Callable[[str, str, int], None]`. The returned function is `send(to, code, ttl_minutes)`; it is blocking and raises `MailError`.

- [ ] **Step 1: Write the failing tests**

`tests/test_mailer.py`:
```python
import smtplib

import pytest

from bot import mailer
from bot.mailer import MailError, build_message, make_gmail_sender


class FakeSMTP:
    instances = []
    fail_with = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.logged_in = None
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        self.logged_in = (user, password)

    def send_message(self, message):
        self.sent.append(message)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_with = None
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)


def test_build_message_contains_code_and_headers():
    msg = build_message("branch@gmail.com", "a@tuc.gr", "123456", 10)
    assert msg["To"] == "a@tuc.gr" and "branch@gmail.com" in msg["From"]
    body = msg.get_content()
    assert "123456" in body and "10 minutes" in body


def test_send_logs_in_and_sends():
    send = make_gmail_sender("branch@gmail.com", "app-pass")
    send("a@tuc.gr", "123456", 10)
    (smtp,) = FakeSMTP.instances
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.gmail.com", 465, 20)
    assert smtp.logged_in == ("branch@gmail.com", "app-pass")
    assert smtp.sent[0]["To"] == "a@tuc.gr"


def test_smtp_errors_become_mail_error():
    FakeSMTP.fail_with = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    with pytest.raises(MailError):
        make_gmail_sender("a@gmail.com", "x")("a@tuc.gr", "123456", 10)


def test_network_errors_become_mail_error():
    FakeSMTP.fail_with = OSError("network down")
    with pytest.raises(MailError):
        make_gmail_sender("a@gmail.com", "x")("a@tuc.gr", "123456", 10)


def test_header_injection_never_reaches_smtp():
    send = make_gmail_sender("a@gmail.com", "x")
    with pytest.raises(MailError):
        send("a@tuc.gr\nBcc: victim@example.com", "123456", 10)
    assert all(not smtp.sent for smtp in FakeSMTP.instances)
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_mailer.py -v`
Expected: FAIL (`ModuleNotFoundError: bot.mailer`).

- [ ] **Step 3: Implement**

`bot/mailer.py`:
```python
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Callable


class MailError(Exception):
    pass


def build_message(sender: str, to: str, code: str, ttl_minutes: int) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"IEEE Student Branch TUC <{sender}>"
    msg["To"] = to  # raises ValueError on a newline, which make_gmail_sender turns into MailError
    msg["Subject"] = "Your IEEE TUC Discord verification code"
    msg.set_content(
        f"Your verification code is: {code}\n\n"
        f"It expires in {ttl_minutes} minutes. If you did not request this, you can ignore this email.\n"
    )
    return msg


def make_gmail_sender(
    address: str, app_password: str, *, host: str = "smtp.gmail.com", port: int = 465
) -> Callable[[str, str, int], None]:
    def send(to: str, code: str, ttl_minutes: int) -> None:
        try:
            message = build_message(address, to, code, ttl_minutes)
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context()) as smtp:
                smtp.login(address, app_password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError, ValueError) as exc:
            raise MailError(str(exc)) from exc

    return send
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/pytest tests/test_mailer.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add bot/mailer.py tests/test_mailer.py
git commit -m "feat: gmail smtp mailer with injection-safe headers"
```

---

### Task 6: Verification UI, role helper and officer check

**Files:**
- Create: `bot/roles.py`, `bot/checks.py`, `bot/cogs/verify.py`, `tests/test_roles.py`, `tests/test_checks.py`, `tests/test_cog_imports.py`

**Interfaces:**
- Consumes: `VerificationService`, `VerificationError` (Task 4), `MailError` (Task 5), `MemberRepo` (Task 3). The cogs read dependencies from `bot.app` (an `AppContext`, built in Task 11) with attributes `settings`, `members`, `verification`, `send_code`.
- Produces: `bot.roles.set_verified_role(guild, member, role_id: int, *, add: bool, reason: str) -> bool` (False on missing role or Discord error, never raises for those); `bot.checks.is_officer(member, officer_role_id: int) -> bool`, `officer_only()` (an `app_commands.check`), `handle_app_command_error(interaction, error)`; `bot.cogs.verify.VerifyCog`, `VerifyView`, `setup(bot)`.

Discord UI cannot be unit-tested without a live gateway. The helpers are tested here; the buttons and modals are covered by the manual run in Task 12 and by an import and construction smoke test.

- [ ] **Step 1: Write the failing tests**

`tests/test_roles.py`:
```python
import asyncio
from types import SimpleNamespace

import discord

from bot.roles import set_verified_role


class FakeMember:
    def __init__(self, error=None):
        self.added, self.removed, self.error = [], [], error

    async def add_roles(self, role, reason=None):
        if self.error:
            raise self.error
        self.added.append(role)

    async def remove_roles(self, role, reason=None):
        if self.error:
            raise self.error
        self.removed.append(role)


def guild_with(role):
    return SimpleNamespace(get_role=lambda role_id: role)


def run(coro):
    return asyncio.run(coro)


def test_adds_role():
    member, role = FakeMember(), object()
    assert run(set_verified_role(guild_with(role), member, 5, add=True, reason="r")) is True
    assert member.added == [role]


def test_removes_role():
    member, role = FakeMember(), object()
    assert run(set_verified_role(guild_with(role), member, 5, add=False, reason="r")) is True
    assert member.removed == [role]


def test_missing_role_returns_false():
    assert run(set_verified_role(guild_with(None), FakeMember(), 5, add=True, reason="r")) is False


def test_forbidden_returns_false():
    forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "missing permissions")
    assert run(set_verified_role(guild_with(object()), FakeMember(forbidden), 5, add=True, reason="r")) is False
```

`tests/test_checks.py`:
```python
from types import SimpleNamespace

from bot.checks import is_officer


def member_with(*role_ids):
    return SimpleNamespace(roles=[SimpleNamespace(id=i) for i in role_ids])


def test_officer_has_the_role():
    assert is_officer(member_with(1, 42), 42) is True


def test_non_officer():
    assert is_officer(member_with(1, 2), 42) is False


def test_user_without_roles_attribute_is_not_officer():
    assert is_officer(SimpleNamespace(), 42) is False
```

`tests/test_cog_imports.py`:
```python
import asyncio
from types import SimpleNamespace


def test_verify_view_builds_with_two_persistent_buttons():
    from bot.cogs.verify import VerifyView

    async def build():
        return VerifyView(SimpleNamespace())

    view = asyncio.run(build())
    ids = {child.custom_id for child in view.children}
    assert ids == {"verify:start", "verify:code"}
    assert view.timeout is None
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_roles.py tests/test_checks.py tests/test_cog_imports.py -v`
Expected: FAIL (missing modules).

- [ ] **Step 3: Implement `bot/roles.py` and `bot/checks.py`**

`bot/roles.py`:
```python
from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)


async def set_verified_role(guild, member, role_id: int, *, add: bool, reason: str) -> bool:
    role = guild.get_role(role_id)
    if role is None:
        log.error("Role %s not found in guild", role_id)
        return False
    try:
        if add:
            await member.add_roles(role, reason=reason)
        else:
            await member.remove_roles(role, reason=reason)
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Could not %s role %s for member %s", "add" if add else "remove", role_id, member)
        return False
    return True
```

`bot/checks.py`:
```python
from __future__ import annotations

import logging

import discord
from discord import app_commands

log = logging.getLogger(__name__)


def is_officer(member, officer_role_id: int) -> bool:
    return any(role.id == officer_role_id for role in getattr(member, "roles", []))


def officer_only():
    def predicate(interaction: discord.Interaction) -> bool:
        return is_officer(interaction.user, interaction.client.app.settings.officer_role_id)

    return app_commands.check(predicate)


async def handle_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CheckFailure):
        message = "Only branch officers can use this command."
    else:
        log.error("Command error", exc_info=error)
        message = "Something went wrong. Please try again or tell an officer."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
```

- [ ] **Step 4: Implement `bot/cogs/verify.py`**

```python
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.checks import officer_only
from bot.mailer import MailError
from bot.roles import set_verified_role
from bot.verification import VerificationError

log = logging.getLogger(__name__)

PANEL_TEXT = (
    "**Verify that you are a TUC student**\n"
    "Press **Verify**, enter your name and your `@tuc.gr` email, and we will email you a 6-digit code. "
    "Then press **Enter code** to unlock the server."
)

CONSENT_TEXT = (
    "To verify you, the IEEE Student Branch of TUC will store your **full name**, your **@tuc.gr email** "
    "and your **Discord ID**. They are used only to confirm membership and are visible only to branch "
    "officers. You can delete your data at any time with `/forget-me`.\n\nPress **I agree** to continue."
)


class StartModal(discord.ui.Modal, title="TUC verification"):
    full_name = discord.ui.TextInput(label="Full name", min_length=2, max_length=100)
    email = discord.ui.TextInput(label="Academic email (@tuc.gr)", placeholder="name@tuc.gr", max_length=100)

    def __init__(self, app):
        super().__init__()
        self.app = app

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            pending = self.app.verification.begin(interaction.user.id, str(self.full_name), str(self.email))
        except VerificationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        try:
            await asyncio.to_thread(self.app.send_code, pending.email, pending.code, pending.ttl_minutes)
        except MailError:
            log.exception("Could not send verification email")
            self.app.verification.abort(interaction.user.id)
            await interaction.followup.send(
                "I couldn't send the email right now. Please try again later or ask an officer.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"A code was sent to **{pending.email}** (check spam too). "
            f"Press **Enter code** within {pending.ttl_minutes} minutes.",
            ephemeral=True,
        )


class CodeModal(discord.ui.Modal, title="Enter your code"):
    code = discord.ui.TextInput(label="6-digit code", min_length=6, max_length=12)

    def __init__(self, app):
        super().__init__()
        self.app = app

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            self.app.verification.confirm(interaction.user.id, str(self.code))
        except VerificationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        ok = await set_verified_role(
            interaction.guild, interaction.user, self.app.settings.verified_role_id,
            add=True, reason="TUC email verified",
        )
        if ok:
            await interaction.followup.send("You are verified. Welcome!", ephemeral=True)
        else:
            await interaction.followup.send(
                "Your email is verified, but I couldn't assign the role. An officer will fix it.", ephemeral=True
            )


class ConsentView(discord.ui.View):
    def __init__(self, app):
        super().__init__(timeout=300)
        self.app = app

    @discord.ui.button(label="I agree", style=discord.ButtonStyle.success)
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(StartModal(self.app))


class VerifyView(discord.ui.View):
    def __init__(self, app):
        super().__init__(timeout=None)
        self.app = app

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, custom_id="verify:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.app.members.get(interaction.user.id):
            await interaction.response.send_message("You are already verified.", ephemeral=True)
            return
        await interaction.response.send_message(CONSENT_TEXT, view=ConsentView(self.app), ephemeral=True)

    @discord.ui.button(label="Enter code", style=discord.ButtonStyle.primary, custom_id="verify:code")
    async def enter_code(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CodeModal(self.app))


class VerifyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(VerifyView(self.bot.app))  # keeps the buttons working after a restart

    @app_commands.command(name="setup-verify", description="Post the verification panel in this channel")
    @app_commands.guild_only()
    @officer_only()
    async def setup_verify(self, interaction: discord.Interaction) -> None:
        await interaction.channel.send(PANEL_TEXT, view=VerifyView(self.bot.app))
        await interaction.response.send_message("Panel posted.", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(VerifyCog(bot))
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `.venv/bin/pytest tests/test_roles.py tests/test_checks.py tests/test_cog_imports.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add bot/roles.py bot/checks.py bot/cogs/verify.py tests/
git commit -m "feat: verification buttons, modals, role helper and officer check"
```

---

### Task 7: Officer commands

**Files:**
- Create: `bot/cogs/officer.py`, `tests/test_officer_cog.py`

**Interfaces:**
- Consumes: `MemberRepo`, `members_to_csv` (Task 3), `clean_text` (Task 2), `VerificationService.abort` (Task 4), `set_verified_role`, `officer_only` (Task 6), `utcnow` (Task 3). Reads `bot.app.settings/members/verification`.
- Produces: `OfficerCog` with `/verify-manual user name note`, `/unverify user`, `/member lookup user`, `/member export`, `/member delete user`, and the member-facing `/forget-me`; `setup(bot)`. The data logic is already covered by Task 3 and Task 4 tests; this task adds a smoke test and the Task 12 manual checks.

- [ ] **Step 1: Write the failing test**

`tests/test_officer_cog.py`:
```python
import asyncio
from types import SimpleNamespace


def test_officer_cog_registers_expected_commands():
    from bot.cogs.officer import OfficerCog

    async def build():
        return OfficerCog(SimpleNamespace(app=SimpleNamespace()))

    cog = asyncio.run(build())
    names = {command.name for command in cog.get_app_commands()}
    assert {"verify-manual", "unverify", "member", "forget-me"} <= names
    member_group = next(c for c in cog.get_app_commands() if c.name == "member")
    assert {c.name for c in member_group.commands} == {"lookup", "export", "delete"}
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/pytest tests/test_officer_cog.py -v`
Expected: FAIL (`ModuleNotFoundError: bot.cogs.officer`).

- [ ] **Step 3: Implement**

`bot/cogs/officer.py`:
```python
from __future__ import annotations

import io

import discord
from discord import app_commands
from discord.ext import commands

from bot.checks import officer_only
from bot.db import utcnow
from bot.members import members_to_csv
from bot.roles import set_verified_role
from bot.validation import clean_text

NO_MENTIONS = discord.AllowedMentions.none()


class OfficerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def app(self):
        return self.bot.app

    member_group = app_commands.Group(name="member", description="Stored member data", guild_only=True)

    @app_commands.command(name="verify-manual", description="Verify a member who has no @tuc.gr email")
    @app_commands.guild_only()
    @officer_only()
    async def verify_manual(self, interaction: discord.Interaction, user: discord.Member, name: str, note: str) -> None:
        if user.bot:
            await interaction.response.send_message("Bots cannot be verified.", ephemeral=True)
            return
        try:
            full_name = clean_text(name, min_len=2, max_len=100, label="Name")
            reason = clean_text(note, min_len=3, max_len=200, label="Note")
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.app.members.add_manual_member(
            discord_id=user.id, full_name=full_name, verified_by=interaction.user.id,
            note=reason, verified_at=utcnow(),
        )
        ok = await set_verified_role(
            interaction.guild, user, self.app.settings.verified_role_id,
            add=True, reason=f"Manual verification by {interaction.user}",
        )
        suffix = "" if ok else " (but I couldn't assign the role: check my permissions)"
        await interaction.response.send_message(
            f"{user.mention} verified manually as **{full_name}**{suffix}.",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @app_commands.command(name="unverify", description="Remove the Verified role (stored data is kept)")
    @app_commands.guild_only()
    @officer_only()
    async def unverify(self, interaction: discord.Interaction, user: discord.Member) -> None:
        ok = await set_verified_role(
            interaction.guild, user, self.app.settings.verified_role_id,
            add=False, reason=f"Unverified by {interaction.user}",
        )
        text = "Role removed." if ok else "I couldn't remove the role: check my permissions."
        await interaction.response.send_message(text, ephemeral=True)

    @member_group.command(name="lookup", description="Show stored data for a user")
    @officer_only()
    async def lookup(self, interaction: discord.Interaction, user: discord.User) -> None:
        member = self.app.members.get(user.id)
        if member is None:
            await interaction.response.send_message("No stored data for that user.", ephemeral=True)
            return
        lines = [
            f"**Name:** {discord.utils.escape_markdown(member.full_name)}",
            f"**Email:** {member.email or '(none, verified manually)'}",
            f"**Method:** {member.method}",
            f"**Verified at:** {member.verified_at}",
        ]
        if member.method == "manual":
            lines.append(f"**Verified by:** <@{member.verified_by}>")
            lines.append(f"**Note:** {discord.utils.escape_markdown(member.note or '')}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS)

    @member_group.command(name="export", description="Download all stored members as CSV")
    @officer_only()
    async def export(self, interaction: discord.Interaction) -> None:
        data = members_to_csv(self.app.members.all()).encode("utf-8")
        await interaction.response.send_message(
            file=discord.File(io.BytesIO(data), filename="members.csv"), ephemeral=True
        )

    @member_group.command(name="delete", description="Delete a user's stored data and remove the role")
    @officer_only()
    async def delete(self, interaction: discord.Interaction, user: discord.User) -> None:
        await self._forget(interaction, user.id)

    @app_commands.command(name="forget-me", description="Delete your stored data and remove your Verified role")
    @app_commands.guild_only()
    async def forget_me(self, interaction: discord.Interaction) -> None:
        await self._forget(interaction, interaction.user.id)

    async def _forget(self, interaction: discord.Interaction, discord_id: int) -> None:
        deleted = self.app.members.delete(discord_id)
        self.app.verification.abort(discord_id)
        member = interaction.guild.get_member(discord_id)
        if member is not None:
            await set_verified_role(
                interaction.guild, member, self.app.settings.verified_role_id,
                add=False, reason="Data deleted",
            )
        await interaction.response.send_message(
            "Stored data deleted." if deleted else "There was no stored data.", ephemeral=True
        )


async def setup(bot) -> None:
    await bot.add_cog(OfficerCog(bot))
```

- [ ] **Step 4: Run it to see it pass**

Run: `.venv/bin/pytest tests/test_officer_cog.py -v`
Expected: PASS. If `get_app_commands()` does not include group commands on this discord.py version, assert on `OfficerCog.member_group.commands` instead; the command set is what matters.

- [ ] **Step 5: Commit**

```bash
git add bot/cogs/officer.py tests/test_officer_cog.py
git commit -m "feat: officer commands for manual verification, lookup, export, delete and forget-me"
```

---

### Task 8: Feed fetching and parsing

**Files:**
- Create: `bot/feeds/parse.py`, `tests/test_feed_parse.py`

**Interfaces:**
- Produces: `FeedError(Exception)`, `FeedItem(item_id: str, title: str, link: str | None, summary: str, image_url: str | None, published: datetime | None)` (frozen dataclass), `truncate(text: str, limit: int) -> str`, `parse_feed(content: bytes) -> list[FeedItem]` (feed order preserved, raises `FeedError` if the content is not a feed), `fetch_feed(url: str) -> bytes` (blocking; raises `FeedError` on HTTP != 200, timeout, oversize), constants `USER_AGENT`, `TIMEOUT_SECONDS`, `MAX_FEED_BYTES`.

- [ ] **Step 1: Write the failing tests**

`tests/test_feed_parse.py`:
```python
import pytest
import requests

from bot.feeds import parse
from bot.feeds.parse import FeedError, fetch_feed, parse_feed, truncate

XPLORE = b"""<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"><channel><title><![CDATA[ IEEE Transactions on Robotics - new TOC ]]></title>
<item>
  <title><![CDATA[Guest Editorial: Event-Based Vision for Robotics]]></title>
  <link><![CDATA[http://ieeexplore.ieee.org/document/11688145]]></link>
  <description><![CDATA[null]]></description>
  <pubDate><![CDATA[FRI, 11 SEP 2026 01:06:42 -0400]]></pubDate>
  <guid><![CDATA[http://ieeexplore.ieee.org/document/11688145]]>
  </guid>
</item></channel></rss>"""

SPECTRUM = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel><title>IEEE Spectrum</title>
<item>
  <title>Robots Learn to Walk</title>
  <link>https://spectrum.ieee.org/robots-walk</link>
  <guid isPermaLink="false">spectrum-1</guid>
  <description><![CDATA[<img src="https://spectrum.ieee.org/media/robot.jpg"/><br/><p>Legged <b>robots</b> are
  getting &amp; better.</p><script>alert(1)</script>]]></description>
  <pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate>
</item></channel></rss>"""


def test_xplore_item_ignores_null_description():
    (item,) = parse_feed(XPLORE)
    assert item.item_id == "http://ieeexplore.ieee.org/document/11688145"
    assert item.title == "Guest Editorial: Event-Based Vision for Robotics"
    assert item.link == "http://ieeexplore.ieee.org/document/11688145"
    assert item.summary == "" and item.image_url is None
    assert item.published is not None and item.published.year == 2026


def test_spectrum_item_strips_html_and_extracts_image():
    (item,) = parse_feed(SPECTRUM)
    assert item.item_id == "spectrum-1"
    assert item.summary == "Legged robots are getting & better."
    assert "alert" not in item.summary
    assert item.image_url == "https://spectrum.ieee.org/media/robot.jpg"


def test_summary_is_truncated_to_300_characters():
    long = SPECTRUM.replace(b"Legged", b"x" * 500)
    (item,) = parse_feed(long)
    assert len(item.summary) <= 300 and item.summary.endswith("…")


def test_html_error_page_is_an_error_not_an_empty_feed():
    with pytest.raises(FeedError):
        parse_feed(b"<html><head><title>I'm a teapot</title></head><body><h1>418</title></body></html>")


def test_plain_text_is_an_error():
    with pytest.raises(FeedError):
        parse_feed(b"I'm a Teapot")


def test_valid_empty_feed_is_an_empty_list():
    assert parse_feed(b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>') == []


def test_non_http_links_are_dropped_and_id_falls_back_to_link():
    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>Bad link</title><link>javascript:alert(1)</link><guid>g1</guid></item>
    <item><title>No guid</title><link>https://example.com/a</link></item>
    <item><title>No id at all</title></item>
    </channel></rss>"""
    items = parse_feed(xml)
    assert [i.item_id for i in items] == ["g1", "https://example.com/a"]
    assert items[0].link is None and items[1].link == "https://example.com/a"


def test_truncate():
    assert truncate("abc", 5) == "abc"
    assert truncate("abcdef", 4) == "abc…"


class FakeResponse:
    def __init__(self, status=200, chunks=(b"data",)):
        self.status_code, self._chunks = status, chunks

    def iter_content(self, size):
        return iter(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_returns_body_and_sends_user_agent(monkeypatch):
    seen = {}

    def fake_get(url, headers, timeout, stream):
        seen.update(url=url, headers=headers, timeout=timeout)
        return FakeResponse(chunks=(b"ab", b"cd"))

    monkeypatch.setattr(parse.requests, "get", fake_get)
    assert fetch_feed("https://x/feed") == b"abcd"
    assert seen["headers"]["User-Agent"] == parse.USER_AGENT and seen["timeout"] == 20


def test_fetch_reports_http_status(monkeypatch):
    monkeypatch.setattr(parse.requests, "get", lambda *a, **k: FakeResponse(status=418))
    with pytest.raises(FeedError, match="418"):
        fetch_feed("https://x/feed")


def test_fetch_wraps_network_errors(monkeypatch):
    def boom(*a, **k):
        raise requests.Timeout("slow")

    monkeypatch.setattr(parse.requests, "get", boom)
    with pytest.raises(FeedError):
        fetch_feed("https://x/feed")


def test_fetch_rejects_oversized_feeds(monkeypatch):
    monkeypatch.setattr(parse, "MAX_FEED_BYTES", 5)
    monkeypatch.setattr(parse.requests, "get", lambda *a, **k: FakeResponse(chunks=(b"abc", b"def")))
    with pytest.raises(FeedError, match="large"):
        fetch_feed("https://x/feed")
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_feed_parse.py -v`
Expected: FAIL (`ModuleNotFoundError: bot.feeds.parse`).

- [ ] **Step 3: Implement**

`bot/feeds/parse.py`:
```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

import feedparser
import requests

USER_AGENT = "IEEE-TUC-DiscordBot/1.0 (student branch RSS reader)"
TIMEOUT_SECONDS = 20
MAX_FEED_BYTES = 5_000_000
SUMMARY_LIMIT = 300


class FeedError(Exception):
    pass


@dataclass(frozen=True)
class FeedItem:
    item_id: str
    title: str
    link: str | None
    summary: str
    image_url: str | None
    published: datetime | None


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.image: str | None = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "img" and self.image is None:
            src = dict(attrs).get("src") or ""
            if src.startswith(("http://", "https://")):
                self.image = src
        elif tag in ("br", "p", "div", "li"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _text_and_image(html: str) -> tuple[str, str | None]:
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return " ".join("".join(extractor.parts).split()), extractor.image


def _http_url(value) -> str | None:
    return value if isinstance(value, str) and value.startswith(("http://", "https://")) else None


def parse_feed(content: bytes) -> list[FeedItem]:
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        raise FeedError("Response is not a valid RSS/Atom feed")
    items: list[FeedItem] = []
    for entry in parsed.entries:
        link = _http_url(entry.get("link"))
        item_id = (entry.get("id") or link or "").strip()
        title = " ".join(str(entry.get("title", "")).split())
        if not item_id or not title:
            continue
        raw = entry.get("summary", "") or ""
        if raw.strip().lower() == "null":  # IEEE Xplore puts the literal text "null" here
            raw = ""
        text, image = _text_and_image(raw)
        if image is None:
            for key in ("media_content", "media_thumbnail"):
                for media in entry.get(key, []) or []:
                    image = _http_url(media.get("url"))
                    if image:
                        break
                if image:
                    break
        stamp = entry.get("published_parsed")
        published = datetime(*stamp[:6], tzinfo=timezone.utc) if stamp else None
        items.append(FeedItem(item_id, title, link, truncate(text, SUMMARY_LIMIT), image, published))
    return items


def fetch_feed(url: str) -> bytes:
    try:
        with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS, stream=True) as response:
            if response.status_code != 200:
                raise FeedError(f"HTTP {response.status_code} from {url}")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_FEED_BYTES:
                    raise FeedError(f"Feed is too large: {url}")
                chunks.append(chunk)
            return b"".join(chunks)
    except requests.RequestException as exc:
        raise FeedError(f"{type(exc).__name__}: {exc}") from exc
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/bin/pytest tests/test_feed_parse.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add bot/feeds/parse.py tests/test_feed_parse.py
git commit -m "feat: feed fetching and parsing, including Xplore null descriptions"
```

---

### Task 9: Feed batching, stores and polling service

**Files:**
- Create: `bot/feeds/batch.py`, `bot/feeds/store.py`, `bot/feeds/service.py`, `tests/test_feed_batch.py`, `tests/test_feed_store.py`, `tests/test_feed_service.py`

**Interfaces:**
- Consumes: `FeedItem`, `parse_feed`, `fetch_feed`, `FeedError`, `truncate` (Task 8), `FeedConfig` (Task 1), `to_iso`/`utcnow` (Task 3).
- Produces from `batch`: `MAX_EMBEDS_PER_MESSAGE = 10`, `MAX_CHARS_PER_MESSAGE = 5500`, `item_to_embed(item: FeedItem, feed_title: str) -> discord.Embed`, `batch_embeds(embeds: list[discord.Embed]) -> list[list[discord.Embed]]`.
- Produces from `store`: `SeenStore(conn)` with `is_initialized(feed_id) -> bool`, `initialize(feed_id, item_ids: list[str], now: datetime) -> None`, `seen_ids(feed_id) -> set[str]`, `mark_seen(feed_id, item_ids: list[str], now: datetime) -> None`; `CustomFeedRepo(conn)` with `add(feed: FeedConfig) -> None` (raises `FeedExists`), `remove(feed_id) -> bool`, `all() -> list[FeedConfig]`; `FeedExists`.
- Produces from `service`: `Selection(to_post: list[FeedItem], overflow_ids: list[str], first_run: bool)`, `select_new(store, feed_id, items, max_items, now) -> Selection`, `async process_feed(feed: FeedConfig, *, fetch, store, send, max_items, clock=utcnow) -> int`. `fetch(url) -> bytes` is blocking; `send(channel_id: int, embeds: list[discord.Embed], content: str | None)` is an async callable. Returns the number of items posted.

- [ ] **Step 1: Verify the Discord message limits**

Read the current discord.py / Discord documentation for the maximum number of embeds per message and the maximum combined embed characters per message (the plan assumes 10 and 6000). Use WebSearch or WebFetch. If either differs, update the two constants in `bot/feeds/batch.py` (Step 4), the tests below, and the Global Constraints line. Record the source URL in the commit message.

- [ ] **Step 2: Write the failing tests**

`tests/test_feed_batch.py`:
```python
from datetime import datetime, timezone

import discord

from bot.feeds.batch import MAX_CHARS_PER_MESSAGE, MAX_EMBEDS_PER_MESSAGE, batch_embeds, item_to_embed
from bot.feeds.parse import FeedItem


def item(**kw):
    base = dict(item_id="1", title="A paper", link="https://x/1", summary="", image_url=None, published=None)
    base.update(kw)
    return FeedItem(**base)


def test_embed_fields():
    stamp = datetime(2026, 10, 4, tzinfo=timezone.utc)
    embed = item_to_embed(
        item(summary="Short summary", image_url="https://x/i.png", published=stamp), "IEEE Transactions on Robotics"
    )
    assert embed.title == "A paper" and embed.url == "https://x/1"
    assert embed.description == "Short summary"
    assert embed.image.url == "https://x/i.png"
    assert embed.footer.text == "IEEE Transactions on Robotics"
    assert embed.timestamp == stamp


def test_empty_summary_and_missing_link_are_allowed():
    embed = item_to_embed(item(summary="", link=None), "J")
    assert embed.description is None and embed.url is None


def test_title_is_truncated_to_256():
    assert len(item_to_embed(item(title="t" * 400), "J").title) <= 256


def test_batches_split_at_ten_embeds():
    embeds = [discord.Embed(title=f"t{i}") for i in range(25)]
    sizes = [len(b) for b in batch_embeds(embeds)]
    assert sizes == [MAX_EMBEDS_PER_MESSAGE, MAX_EMBEDS_PER_MESSAGE, 5]


def test_batches_split_by_text_size_and_keep_order():
    big = [discord.Embed(title=f"t{i}", description="x" * 3000) for i in range(3)]
    batches = batch_embeds(big)
    assert [len(b) for b in batches] == [1, 1, 1]
    assert [e.title for b in batches for e in b] == ["t0", "t1", "t2"]
    assert all(sum(len(e) for e in b) <= MAX_CHARS_PER_MESSAGE for b in batches)


def test_no_embeds_no_batches():
    assert batch_embeds([]) == []
```

`tests/test_feed_store.py`:
```python
from datetime import datetime, timezone

import pytest

from bot.config import FeedConfig
from bot.feeds.store import CustomFeedRepo, FeedExists, SeenStore

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def test_initialize_marks_items_seen(conn):
    store = SeenStore(conn)
    assert store.is_initialized("f") is False
    store.initialize("f", ["a", "b"], NOW)
    assert store.is_initialized("f") is True
    assert store.seen_ids("f") == {"a", "b"}
    assert store.seen_ids("other") == set()


def test_mark_seen_is_idempotent(conn):
    store = SeenStore(conn)
    store.mark_seen("f", ["a"], NOW)
    store.mark_seen("f", ["a", "b"], NOW)
    assert store.seen_ids("f") == {"a", "b"}


def test_custom_feeds_add_list_remove(conn):
    repo = CustomFeedRepo(conn)
    feed = FeedConfig("my-feed", "My Feed", "https://x/feed", 123)
    repo.add(feed)
    assert repo.all() == [feed]
    with pytest.raises(FeedExists):
        repo.add(feed)
    assert repo.remove("my-feed") is True
    assert repo.remove("my-feed") is False
    assert repo.all() == []
```

`tests/test_feed_service.py`:
```python
import asyncio
from datetime import datetime, timezone

import pytest

from bot.config import FeedConfig
from bot.feeds.parse import FeedError
from bot.feeds.service import process_feed
from bot.feeds.store import SeenStore

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
FEED = FeedConfig("robotics-tr", "IEEE Transactions on Robotics", "https://x/feed", 77)


def feed_xml(ids, *, oldest_first=False):
    ids = list(ids)
    entries = []
    for i, item_id in enumerate(ids):
        # ids later in the list are newer: pubDate grows with the numeric suffix
        day = 1 + int(str(item_id).split("-")[-1]) if "-" in str(item_id) else 1 + i
        entries.append(
            f"<item><title>Paper {item_id}</title><link>https://x/{item_id}</link><guid>{item_id}</guid>"
            f"<pubDate>Mon, {day:02d} Sep 2026 10:00:00 GMT</pubDate></item>"
        )
    if not oldest_first:
        entries.reverse()
    return ('<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>' + "".join(entries) + "</channel></rss>").encode()


class Harness:
    def __init__(self, conn, content):
        self.store = SeenStore(conn)
        self.content = content
        self.sent = []
        self.fail_on_call = None

    def fetch(self, url):
        if isinstance(self.content, Exception):
            raise self.content
        return self.content

    async def send(self, channel_id, embeds, content):
        if self.fail_on_call is not None and len(self.sent) + 1 == self.fail_on_call:
            raise RuntimeError("discord is down")
        self.sent.append((channel_id, [e.title for e in embeds], content))

    def run(self, max_items=10):
        return asyncio.run(process_feed(
            FEED, fetch=self.fetch, store=self.store, send=self.send, max_items=max_items, clock=lambda: NOW
        ))


def titles(h):
    return [t for _, batch, _ in h.sent for t in batch]


def test_first_run_posts_nothing_then_posts_only_new_items(conn):
    h = Harness(conn, feed_xml(["p-1", "p-2", "p-3"]))
    assert h.run() == 0 and h.sent == []
    h.content = feed_xml(["p-1", "p-2", "p-3", "p-4"])
    assert h.run() == 1
    assert titles(h) == ["Paper p-4"] and h.sent[0][0] == 77


def test_unchanged_feed_posts_nothing_even_after_many_polls(conn):
    h = Harness(conn, feed_xml(["p-1", "p-2"]))
    h.run()
    for _ in range(3):
        assert h.run() == 0
    assert h.sent == []


def test_cap_posts_newest_in_chronological_order_and_notes_overflow(conn):
    h = Harness(conn, feed_xml(["p-1"]))
    h.run()
    h.content = feed_xml([f"p-{i}" for i in range(1, 15)])      # 13 new items
    assert h.run(max_items=10) == 10
    assert titles(h) == [f"Paper p-{i}" for i in range(5, 15)]  # newest 10, oldest of them first
    assert "+3" in h.sent[-1][2] and FEED.title in h.sent[-1][2]
    assert h.run(max_items=10) == 0                              # overflow was marked seen


def test_oldest_first_feeds_still_keep_the_newest(conn):
    h = Harness(conn, feed_xml(["p-1"], oldest_first=True))
    h.run()
    h.content = feed_xml([f"p-{i}" for i in range(1, 6)], oldest_first=True)
    assert h.run(max_items=2) == 2
    assert titles(h) == ["Paper p-4", "Paper p-5"]


def test_duplicate_ids_in_a_feed_are_posted_once(conn):
    h = Harness(conn, feed_xml(["p-1"]))
    h.run()
    h.content = feed_xml(["p-1", "p-2", "p-2"])
    assert h.run() == 1


def test_html_error_page_raises_and_does_not_initialise(conn):
    h = Harness(conn, b"<html><body>418</body></html>")
    with pytest.raises(FeedError):
        h.run()
    assert h.store.is_initialized(FEED.id) is False
    h.content = feed_xml(["p-1", "p-2"])
    assert h.run() == 0                       # still treated as the first run
    assert h.store.is_initialized(FEED.id) is True


def test_fetch_errors_propagate_and_change_nothing(conn):
    h = Harness(conn, FeedError("HTTP 418"))
    with pytest.raises(FeedError, match="418"):
        h.run()
    assert h.store.seen_ids(FEED.id) == set()


def test_send_failure_keeps_items_unseen_and_they_are_retried(conn):
    h = Harness(conn, feed_xml(["p-1"]))
    h.run()
    h.content = feed_xml(["p-1", "p-2"])
    h.fail_on_call = 1
    with pytest.raises(RuntimeError):
        h.run()
    assert h.store.seen_ids(FEED.id) == {"p-1"}
    h.fail_on_call = None
    assert h.run() == 1 and titles(h) == ["Paper p-2"]


def test_partial_failure_marks_only_posted_batches(conn):
    h = Harness(conn, feed_xml(["p-1"]))
    h.run()
    h.content = feed_xml([f"p-{i}" for i in range(1, 16)])      # 14 new items
    h.fail_on_call = 2                                          # 10 + 4: second message fails
    with pytest.raises(RuntimeError):
        h.run(max_items=14)
    assert len(titles(h)) == 10
    h.fail_on_call = None
    assert h.run(max_items=14) == 4                             # only the 4 that failed
    assert len(set(titles(h))) == 14                            # nothing posted twice
```

- [ ] **Step 3: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_feed_batch.py tests/test_feed_store.py tests/test_feed_service.py -v`
Expected: FAIL (missing modules).

- [ ] **Step 4: Implement `bot/feeds/batch.py`**

```python
from __future__ import annotations

import discord

from bot.feeds.parse import FeedItem, truncate

MAX_EMBEDS_PER_MESSAGE = 10
MAX_CHARS_PER_MESSAGE = 5500  # Discord's limit is 6000; keep a safety margin
IEEE_BLUE = 0x00629B


def item_to_embed(item: FeedItem, feed_title: str) -> discord.Embed:
    embed = discord.Embed(
        title=truncate(item.title, 256),
        url=item.link,
        description=item.summary or None,
        timestamp=item.published,
        colour=discord.Colour(IEEE_BLUE),
    )
    embed.set_footer(text=truncate(feed_title, 100))
    if item.image_url:
        embed.set_image(url=item.image_url)
    return embed


def batch_embeds(embeds: list[discord.Embed]) -> list[list[discord.Embed]]:
    batches: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    size = 0
    for embed in embeds:
        length = len(embed)
        if current and (len(current) >= MAX_EMBEDS_PER_MESSAGE or size + length > MAX_CHARS_PER_MESSAGE):
            batches.append(current)
            current, size = [], 0
        current.append(embed)
        size += length
    if current:
        batches.append(current)
    return batches
```

- [ ] **Step 5: Implement `bot/feeds/store.py`**

```python
from __future__ import annotations

import sqlite3
from datetime import datetime

from bot.config import FeedConfig
from bot.db import to_iso


class FeedExists(Exception):
    pass


class SeenStore:
    def __init__(self, conn: sqlite3.Connection):
        self._c = conn

    def is_initialized(self, feed_id: str) -> bool:
        row = self._c.execute("SELECT 1 FROM feed_state WHERE feed_id = ?", (feed_id,)).fetchone()
        return row is not None

    def initialize(self, feed_id: str, item_ids: list[str], now: datetime) -> None:
        with self._c:
            self._insert_seen(feed_id, item_ids, now)
            self._c.execute(
                "INSERT OR IGNORE INTO feed_state (feed_id, initialized_at) VALUES (?, ?)", (feed_id, to_iso(now))
            )

    def seen_ids(self, feed_id: str) -> set[str]:
        rows = self._c.execute("SELECT item_id FROM seen_items WHERE feed_id = ?", (feed_id,)).fetchall()
        return {row["item_id"] for row in rows}

    def mark_seen(self, feed_id: str, item_ids: list[str], now: datetime) -> None:
        with self._c:
            self._insert_seen(feed_id, item_ids, now)

    def _insert_seen(self, feed_id: str, item_ids: list[str], now: datetime) -> None:
        self._c.executemany(
            "INSERT OR IGNORE INTO seen_items (feed_id, item_id, posted_at) VALUES (?, ?, ?)",
            [(feed_id, item_id, to_iso(now)) for item_id in item_ids],
        )


class CustomFeedRepo:
    def __init__(self, conn: sqlite3.Connection):
        self._c = conn

    def add(self, feed: FeedConfig) -> None:
        try:
            with self._c:
                self._c.execute(
                    "INSERT INTO custom_feeds (id, title, url, channel_id) VALUES (?, ?, ?, ?)",
                    (feed.id, feed.title, feed.url, feed.channel_id),
                )
        except sqlite3.IntegrityError:
            raise FeedExists(feed.id) from None

    def remove(self, feed_id: str) -> bool:
        with self._c:
            cursor = self._c.execute("DELETE FROM custom_feeds WHERE id = ?", (feed_id,))
        return cursor.rowcount > 0

    def all(self) -> list[FeedConfig]:
        rows = self._c.execute("SELECT id, title, url, channel_id FROM custom_feeds ORDER BY id").fetchall()
        return [FeedConfig(r["id"], r["title"], r["url"], r["channel_id"]) for r in rows]
```

- [ ] **Step 6: Implement `bot/feeds/service.py`**

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

import discord

from bot.config import FeedConfig
from bot.db import utcnow
from bot.feeds.batch import batch_embeds, item_to_embed
from bot.feeds.parse import FeedItem, parse_feed
from bot.feeds.store import SeenStore

_OLDEST = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Selection:
    to_post: list[FeedItem]      # chronological order, oldest first
    overflow_ids: list[str]      # new items beyond the cap: never posted, marked seen
    first_run: bool


def select_new(store: SeenStore, feed_id: str, items: list[FeedItem], max_items: int, now: datetime) -> Selection:
    unique: dict[str, FeedItem] = {}
    for item in items:
        unique.setdefault(item.item_id, item)
    if not store.is_initialized(feed_id):
        store.initialize(feed_id, list(unique), now)
        return Selection([], [], first_run=True)
    seen = store.seen_ids(feed_id)
    new = [item for item in unique.values() if item.item_id not in seen]
    new.sort(key=lambda item: item.published or _OLDEST, reverse=True)  # stable: ties keep feed order
    kept, overflow = new[:max_items], new[max_items:]
    return Selection(list(reversed(kept)), [item.item_id for item in overflow], first_run=False)


async def process_feed(
    feed: FeedConfig,
    *,
    fetch: Callable[[str], bytes],
    store: SeenStore,
    send: Callable[[int, list[discord.Embed], str | None], Awaitable[None]],
    max_items: int,
    clock: Callable[[], datetime] = utcnow,
) -> int:
    content = await asyncio.to_thread(fetch, feed.url)
    items = parse_feed(content)
    selection = select_new(store, feed.id, items, max_items, clock())
    if selection.first_run or not (selection.to_post or selection.overflow_ids):
        return 0

    note = None
    if selection.overflow_ids:
        note = f"+{len(selection.overflow_ids)} more new items from **{feed.title}** were not shown."
    embeds = [item_to_embed(item, feed.title) for item in selection.to_post]
    batches = batch_embeds(embeds)
    start = 0
    for index, batch in enumerate(batches):
        last = index == len(batches) - 1
        await send(feed.channel_id, batch, note if last else None)
        posted = selection.to_post[start:start + len(batch)]
        store.mark_seen(feed.id, [item.item_id for item in posted], clock())  # only after a successful send
        start += len(batch)
    store.mark_seen(feed.id, selection.overflow_ids, clock())
    return len(selection.to_post)
```

- [ ] **Step 7: Run the tests to see them pass**

Run: `.venv/bin/pytest tests/test_feed_batch.py tests/test_feed_store.py tests/test_feed_service.py -v`
Expected: all pass. The `feed_xml` helper derives dates from the numeric suffix of ids like `p-4`, so "newer" means a larger number.

- [ ] **Step 8: Commit**

```bash
git add bot/feeds tests/
git commit -m "feat: embed batching, seen-item store and feed polling service"
```

---

### Task 10: Feeds cog (poll loop and /feed commands)

**Files:**
- Create: `bot/cogs/feeds.py`, `tests/test_feeds_cog.py`

**Interfaces:**
- Consumes: `process_feed` (Task 9), `fetch_feed`, `parse_feed`, `FeedError` (Task 8), `SeenStore`, `CustomFeedRepo`, `FeedExists` (Task 9), `FeedConfig` (Task 1), `officer_only` (Task 6). Reads `bot.app.settings/seen/custom_feeds`.
- Produces: `FeedsCog` with `all_feeds() -> list[FeedConfig]` (config feeds first, custom feeds with unused ids appended), a `poll` loop every `poll_interval_minutes`, commands `/feed list`, `/feed add id title url channel`, `/feed remove id`; `setup(bot)`; `FEED_ID_RE`.

- [ ] **Step 1: Write the failing test**

`tests/test_feeds_cog.py`:
```python
import asyncio
from types import SimpleNamespace

from bot.config import FeedConfig
from bot.feeds.store import CustomFeedRepo


def make_app(conn, config_feeds):
    return SimpleNamespace(
        settings=SimpleNamespace(feeds=tuple(config_feeds), poll_interval_minutes=30, max_items_per_poll=10),
        seen=None,
        custom_feeds=CustomFeedRepo(conn),
    )


def test_all_feeds_merges_config_and_custom_with_config_winning(conn):
    from bot.cogs.feeds import FeedsCog

    app = make_app(conn, [FeedConfig("a", "A", "https://x/a", 1)])
    app.custom_feeds.add(FeedConfig("a", "Dup", "https://x/dup", 2))
    app.custom_feeds.add(FeedConfig("b", "B", "https://x/b", 3))

    async def build():
        return FeedsCog(SimpleNamespace(app=app))

    cog = asyncio.run(build())
    assert [(f.id, f.url) for f in cog.all_feeds()] == [("a", "https://x/a"), ("b", "https://x/b")]


def test_feed_id_pattern():
    from bot.cogs.feeds import FEED_ID_RE

    assert FEED_ID_RE.fullmatch("ai-ml-2")
    assert not FEED_ID_RE.fullmatch("Has Space")
    assert not FEED_ID_RE.fullmatch("x" * 41)
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/pytest tests/test_feeds_cog.py -v`
Expected: FAIL (`ModuleNotFoundError: bot.cogs.feeds`).

- [ ] **Step 3: Implement**

`bot/cogs/feeds.py`:
```python
from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.checks import officer_only
from bot.config import FeedConfig
from bot.db import utcnow
from bot.feeds.parse import FeedError, fetch_feed, parse_feed
from bot.feeds.service import process_feed
from bot.feeds.store import FeedExists

log = logging.getLogger(__name__)

FEED_ID_RE = re.compile(r"[a-z0-9][a-z0-9\-]{0,39}")


class FeedsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.poll.change_interval(minutes=self.app.settings.poll_interval_minutes)

    @property
    def app(self):
        return self.bot.app

    feed_group = app_commands.Group(name="feed", description="Manage RSS feeds", guild_only=True)

    async def cog_load(self) -> None:
        self.poll.start()

    async def cog_unload(self) -> None:
        self.poll.cancel()

    def all_feeds(self) -> list[FeedConfig]:
        feeds = list(self.app.settings.feeds)
        used = {feed.id for feed in feeds}
        feeds.extend(feed for feed in self.app.custom_feeds.all() if feed.id not in used)
        return feeds

    async def send(self, channel_id: int, embeds: list[discord.Embed], content: str | None) -> None:
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        await channel.send(content=content, embeds=embeds, allowed_mentions=discord.AllowedMentions.none())

    async def poll_feed(self, feed: FeedConfig) -> int:
        return await process_feed(
            feed, fetch=fetch_feed, store=self.app.seen, send=self.send,
            max_items=self.app.settings.max_items_per_poll,
        )

    @tasks.loop(minutes=30)
    async def poll(self) -> None:
        for feed in self.all_feeds():
            try:
                posted = await self.poll_feed(feed)
                log.info("Feed %s: %d new item(s) posted", feed.id, posted)
            except FeedError as exc:
                log.warning("Feed %s failed: %s", feed.id, exc)
            except Exception:
                log.exception("Feed %s crashed", feed.id)

    @poll.before_loop
    async def before_poll(self) -> None:
        await self.bot.wait_until_ready()

    @feed_group.command(name="list", description="List the configured feeds")
    @officer_only()
    async def list_feeds(self, interaction: discord.Interaction) -> None:
        custom_ids = {feed.id for feed in self.app.custom_feeds.all()}
        lines = [
            f"`{feed.id}` {feed.title} -> <#{feed.channel_id}>" + (" (added by command)" if feed.id in custom_ids else "")
            for feed in self.all_feeds()
        ]
        await interaction.response.send_message("\n".join(lines) or "No feeds.", ephemeral=True)

    @feed_group.command(name="add", description="Add an RSS feed")
    @officer_only()
    async def add_feed(
        self, interaction: discord.Interaction, id: str, title: str, url: str, channel: discord.TextChannel
    ) -> None:
        if not FEED_ID_RE.fullmatch(id):
            await interaction.response.send_message(
                "The id must be 1-40 characters: lowercase letters, digits and `-`.", ephemeral=True
            )
            return
        if not url.startswith("https://"):
            await interaction.response.send_message("The URL must start with https://", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            content = await asyncio.to_thread(fetch_feed, url)
            parse_feed(content)
        except FeedError as exc:
            await interaction.followup.send(f"I couldn't read that feed: {exc}", ephemeral=True)
            return
        feed = FeedConfig(id, title[:100], url, channel.id)
        if id in {f.id for f in self.app.settings.feeds}:
            await interaction.followup.send("That id is used by a feed in the config file.", ephemeral=True)
            return
        try:
            self.app.custom_feeds.add(feed)
        except FeedExists:
            await interaction.followup.send("A feed with that id already exists.", ephemeral=True)
            return
        try:
            await self.poll_feed(feed)  # first run: marks current items as seen, posts nothing
        except FeedError as exc:
            log.warning("Initial poll of new feed %s failed: %s", id, exc)
        await interaction.followup.send(
            f"Added `{id}`. Existing items were skipped; new ones will appear in {channel.mention}.", ephemeral=True
        )

    @feed_group.command(name="remove", description="Remove a feed that was added with /feed add")
    @officer_only()
    async def remove_feed(self, interaction: discord.Interaction, id: str) -> None:
        if id in {f.id for f in self.app.settings.feeds}:
            await interaction.response.send_message("That feed is in config.toml; remove it there.", ephemeral=True)
            return
        removed = self.app.custom_feeds.remove(id)
        await interaction.response.send_message("Removed." if removed else "No such feed.", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(FeedsCog(bot))
```

- [ ] **Step 4: Run it to see it pass**

Run: `.venv/bin/pytest tests/test_feeds_cog.py -v`
Expected: 2 passed. If `FeedsCog(...)` construction fails because `poll` needs a running loop, the test's `asyncio.run(build())` already supplies one.

- [ ] **Step 5: Commit**

```bash
git add bot/cogs/feeds.py tests/test_feeds_cog.py
git commit -m "feat: feeds cog with poll loop and /feed add, remove, list"
```

---

### Task 11: Backup, app wiring, entry point and deployment files

**Files:**
- Create: `bot/backup.py`, `bot/app.py`, `bot/main.py`, `bot/cogs/maintenance.py`, `scripts/check_feeds.py`, `config.example.toml`, `Dockerfile`, `docker-compose.yml`, `README.md`, `tests/test_backup.py`, `tests/test_app.py`

**Interfaces:**
- Consumes: every earlier module.
- Produces: `backup_database(conn, backup_dir: Path, *, keep: int = 7, now: datetime | None = None) -> Path`; `AppContext` dataclass (`settings, conn, members, verification, send_code, seen, custom_feeds`); `build_app(settings: Settings) -> AppContext`; `IEEEBot(commands.Bot)` with `.app`; `main()`; `MaintenanceCog` (nightly backup at 03:00 UTC).

- [ ] **Step 1: Write the failing tests**

`tests/test_backup.py`:
```python
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone

from bot.backup import backup_database

NOW = datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc)


def test_backup_is_a_private_readable_copy(conn, members, tmp_path):
    members.add_manual_member(discord_id=1, full_name="Prof X", verified_by=2, note="Professor", verified_at=NOW)
    dest = backup_database(conn, tmp_path / "backups", now=NOW)
    assert dest.name == "bot-20261004.sqlite3"
    assert stat.S_IMODE(os.stat(dest).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(dest.parent).st_mode) == 0o700
    copy = sqlite3.connect(dest)
    assert copy.execute("SELECT full_name FROM members").fetchone()[0] == "Prof X"
    copy.close()


def test_only_the_last_seven_backups_are_kept(conn, tmp_path):
    folder = tmp_path / "backups"
    for day in range(9):
        backup_database(conn, folder, now=NOW + timedelta(days=day))
    names = sorted(p.name for p in folder.glob("bot-*.sqlite3"))
    assert len(names) == 7
    assert names[0] == "bot-20261006.sqlite3" and names[-1] == "bot-20261012.sqlite3"


def test_backing_up_twice_in_a_day_overwrites(conn, tmp_path):
    folder = tmp_path / "backups"
    backup_database(conn, folder, now=NOW)
    backup_database(conn, folder, now=NOW)
    assert len(list(folder.glob("bot-*.sqlite3"))) == 1
```

`tests/test_app.py`:
```python
from pathlib import Path

from bot.app import build_app
from bot.config import Settings


def test_build_app_wires_services(tmp_path):
    settings = Settings(
        discord_token="t", gmail_address="a@gmail.com", gmail_app_password="p",
        guild_id=1, verified_role_id=2, officer_role_id=3,
        db_path=tmp_path / "bot.sqlite3", backup_dir=tmp_path / "backups",
        poll_interval_minutes=30, max_items_per_poll=10, feeds=(),
    )
    app = build_app(settings)
    assert app.settings is settings
    assert callable(app.send_code)
    assert app.members.all() == []
    assert app.custom_feeds.all() == []
    app.conn.close()
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_backup.py tests/test_app.py -v`
Expected: FAIL (missing modules).

- [ ] **Step 3: Implement `bot/backup.py` and `bot/app.py`**

`bot/backup.py`:
```python
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from bot.db import utcnow


def backup_database(conn: sqlite3.Connection, backup_dir: Path, *, keep: int = 7, now: datetime | None = None) -> Path:
    now = now or utcnow()
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest = backup_dir / f"bot-{now:%Y%m%d}.sqlite3"
    os.close(os.open(dest, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600))
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    os.chmod(dest, 0o600)
    for old in sorted(backup_dir.glob("bot-*.sqlite3"))[:-keep]:
        old.unlink()
    return dest
```

`bot/app.py`:
```python
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from bot.config import Settings
from bot.db import connect
from bot.feeds.store import CustomFeedRepo, SeenStore
from bot.mailer import make_gmail_sender
from bot.members import MemberRepo
from bot.verification import VerificationService


@dataclass
class AppContext:
    settings: Settings
    conn: sqlite3.Connection
    members: MemberRepo
    verification: VerificationService
    send_code: Callable[[str, str, int], None]
    seen: SeenStore
    custom_feeds: CustomFeedRepo


def build_app(settings: Settings) -> AppContext:
    conn = connect(settings.db_path)
    members = MemberRepo(conn)
    return AppContext(
        settings=settings,
        conn=conn,
        members=members,
        verification=VerificationService(conn, members),
        send_code=make_gmail_sender(settings.gmail_address, settings.gmail_app_password),
        seen=SeenStore(conn),
        custom_feeds=CustomFeedRepo(conn),
    )
```

- [ ] **Step 4: Implement the maintenance cog and entry point**

`bot/cogs/maintenance.py`:
```python
from __future__ import annotations

import logging
from datetime import time, timezone

from discord.ext import commands, tasks

from bot.backup import backup_database

log = logging.getLogger(__name__)


class MaintenanceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.nightly_backup.start()

    async def cog_unload(self) -> None:
        self.nightly_backup.cancel()

    @tasks.loop(time=time(hour=3, minute=0, tzinfo=timezone.utc))
    async def nightly_backup(self) -> None:
        app = self.bot.app
        try:
            path = backup_database(app.conn, app.settings.backup_dir)
            log.info("Database backed up to %s", path)
        except Exception:
            log.exception("Database backup failed")


async def setup(bot) -> None:
    await bot.add_cog(MaintenanceCog(bot))
```

`bot/main.py`:
```python
from __future__ import annotations

import logging
import os
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from bot.app import AppContext, build_app
from bot.checks import handle_app_command_error
from bot.config import load_settings

EXTENSIONS = ("bot.cogs.verify", "bot.cogs.officer", "bot.cogs.feeds", "bot.cogs.maintenance")


class IEEEBot(commands.Bot):
    def __init__(self, app: AppContext):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.app = app
        self.tree.on_error = handle_app_command_error

    async def setup_hook(self) -> None:
        for extension in EXTENSIONS:
            await self.load_extension(extension)
        guild = discord.Object(id=self.app.settings.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(Path(os.environ.get("BOT_CONFIG", "config.toml")))
    IEEEBot(build_app(settings)).run(settings.discord_token)


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Add the feed reachability script**

`scripts/check_feeds.py`:
```python
"""Run on the server: python -m scripts.check_feeds [config.toml]

Fetches every configured feed and prints the HTTP result, so a block by IEEE shows up before the bot runs.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from bot.config import load_settings
from bot.feeds.parse import FeedError, fetch_feed, parse_feed

DUMMY_ENV = {"DISCORD_TOKEN": "x", "GMAIL_ADDRESS": "x", "GMAIL_APP_PASSWORD": "x"}


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "config.toml")
    settings = load_settings(path, DUMMY_ENV)
    failures = 0
    for feed in settings.feeds:
        try:
            items = parse_feed(fetch_feed(feed.url))
            print(f"OK    {feed.id:<28} {len(items):>3} items  {feed.title}")
        except FeedError as exc:
            failures += 1
            print(f"FAIL  {feed.id:<28} {exc}")
        time.sleep(1)  # be polite to the server
    print(f"\n{len(settings.feeds) - failures}/{len(settings.feeds)} feeds reachable")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Add config and deployment files**

`config.example.toml` (copy to `config.toml` and fill the IDs):
```toml
guild_id = 0
verified_role_id = 0
officer_role_id = 0
db_path = "data/bot.sqlite3"
backup_dir = "data/backups"
poll_interval_minutes = 30
max_items_per_poll = 10

[channels]
ieee-spectrum = 0
ai-ml = 0
power-energy = 0
robotics = 0

[[feeds]]
id = "spectrum"
title = "IEEE Spectrum"
url = "https://spectrum.ieee.org/feeds/feed.rss"
channel = "ieee-spectrum"

[[feeds]]
id = "tpami"
title = "IEEE Transactions on Pattern Analysis and Machine Intelligence"
url = "https://ieeexplore.ieee.org/rss/TOC34.XML"
channel = "ai-ml"

[[feeds]]
id = "tnnls"
title = "IEEE Transactions on Neural Networks and Learning Systems"
url = "https://ieeexplore.ieee.org/rss/TOC5962385.XML"
channel = "ai-ml"

[[feeds]]
id = "tai"
title = "IEEE Transactions on Artificial Intelligence"
url = "https://ieeexplore.ieee.org/rss/TOC9078688.XML"
channel = "ai-ml"

[[feeds]]
id = "tcyb"
title = "IEEE Transactions on Cybernetics"
url = "https://ieeexplore.ieee.org/rss/TOC6221036.XML"
channel = "ai-ml"

[[feeds]]
id = "tpwrs"
title = "IEEE Transactions on Power Systems"
url = "https://ieeexplore.ieee.org/rss/TOC59.XML"
channel = "power-energy"

[[feeds]]
id = "tpel"
title = "IEEE Transactions on Power Electronics"
url = "https://ieeexplore.ieee.org/rss/TOC63.XML"
channel = "power-energy"

[[feeds]]
id = "tsg"
title = "IEEE Transactions on Smart Grid"
url = "https://ieeexplore.ieee.org/rss/TOC5165411.XML"
channel = "power-energy"

[[feeds]]
id = "tste"
title = "IEEE Transactions on Sustainable Energy"
url = "https://ieeexplore.ieee.org/rss/TOC5165391.XML"
channel = "power-energy"

[[feeds]]
id = "tro"
title = "IEEE Transactions on Robotics"
url = "https://ieeexplore.ieee.org/rss/TOC8860.XML"
channel = "robotics"

[[feeds]]
id = "ral"
title = "IEEE Robotics and Automation Letters"
url = "https://ieeexplore.ieee.org/rss/TOC7083369.XML"
channel = "robotics"

[[feeds]]
id = "tase"
title = "IEEE Transactions on Automation Science and Engineering"
url = "https://ieeexplore.ieee.org/rss/TOC8856.XML"
channel = "robotics"

[[feeds]]
id = "tmech"
title = "IEEE/ASME Transactions on Mechatronics"
url = "https://ieeexplore.ieee.org/rss/TOC3516.XML"
channel = "robotics"

[[feeds]]
id = "ram"
title = "IEEE Robotics and Automation Magazine"
url = "https://ieeexplore.ieee.org/rss/TOC100.XML"
channel = "robotics"
```

`Dockerfile`:
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY scripts ./scripts
RUN useradd --create-home --uid 1000 bot
USER bot
CMD ["python", "-m", "bot.main"]
```

`docker-compose.yml`:
```yaml
services:
  bot:
    build: .
    restart: unless-stopped
    env_file: .env
    environment:
      BOT_CONFIG: /app/config.toml
    volumes:
      - ./config.toml:/app/config.toml:ro
      - ./data:/app/data
```

`README.md`:
````markdown
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
   (enable Developer Mode in Discord, then right-click -> Copy ID).
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
`data/backups/` (last 7 kept). Only officers can read or export them.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```
````

- [ ] **Step 7: Run the whole suite**

Run: `.venv/bin/pytest -v`
Expected: every test in every file passes.

- [ ] **Step 8: Verify the entry point wires up without a live Discord connection**

Run:
```bash
.venv/bin/python - <<'EOF'
import dataclasses, tempfile
from pathlib import Path
from bot.app import build_app
from bot.config import load_settings
from bot.main import IEEEBot

env = {"DISCORD_TOKEN": "x", "GMAIL_ADDRESS": "a@gmail.com", "GMAIL_APP_PASSWORD": "p"}
settings = load_settings(Path("config.example.toml"), env)
print(len(settings.feeds), "feeds")
tmp = Path(tempfile.mkdtemp())
settings = dataclasses.replace(settings, db_path=tmp / "bot.sqlite3", backup_dir=tmp / "backups")
IEEEBot(build_app(settings))
print("bot built")
EOF
```
Expected: prints `14 feeds` (1 Spectrum, 4 AI, 4 power, 5 robotics) and `bot built`, with no traceback.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat: nightly backup, app wiring, entry point, deployment files and feed check script"
```

---

### Task 12: End-to-end check on a test server

**Files:** none changed unless a defect is found (fix it with a failing test first, then commit separately).

This is the gate before the bot goes live. The Discord UI and the real SMTP and RSS paths are only exercised here.

- [ ] **Step 1: Prepare a throwaway Discord server and a test config**

Create a test server with roles `Verified` and `Officer` (give yourself `Officer`), channels `#verify`, `#ieee-spectrum`, `#ai-ml`, `#power-energy`, `#robotics`. Create `config.toml` with their IDs and `poll_interval_minutes = 1`. Fill `.env`. Run `python -m scripts.check_feeds` and confirm every line is `OK`.

- [ ] **Step 2: Start the bot and check the first-run rule**

Run: `.venv/bin/python -m bot.main`
Expected: logs show the commands syncing and `Feed <id>: 0 new item(s) posted` for every feed. No channel receives posts. Confirm slash commands appear in the test server.

- [ ] **Step 3: Verify by email**

In `#verify` run `/setup-verify`. With a second account (no Officer role): press **Verify** -> consent text appears -> **I agree** -> enter a name and a real `@tuc.gr` address (or a `@isc.tuc.gr` one). Confirm: the email arrives; **Enter code** with a wrong code says `4 attempt(s) left`; with the right code the member gets `Verified`. Pressing **Verify** again says already verified. Use `/member lookup` as officer and confirm name and email.

- [ ] **Step 4: Check the refusals**

With the second account after `/forget-me`: try `x@gmail.com` and `x@evil-tuc.gr` (both refused with the tuc.gr message); request a code twice within 60 s (cooldown message); let a code expire (10 min) and confirm the expired message.

- [ ] **Step 5: Check manual verification and officer commands**

As officer: `/verify-manual` for a third account with a note; confirm the role and that `/member lookup` shows `manual`, the officer and the note. Run `/member export` and open the CSV. Run `/unverify` (role removed, data kept) and `/member delete` (data gone). Confirm a non-officer gets "Only branch officers can use this command" for each officer command.

- [ ] **Step 6: Check feed posting**

Run `/feed add id:test title:Test url:<a feed that updates often, e.g. https://spectrum.ieee.org/feeds/feed.rss> channel:#ai-ml`. Then stop the bot, run `sqlite3 data/bot.sqlite3 "DELETE FROM seen_items WHERE feed_id='test' AND rowid IN (SELECT rowid FROM seen_items WHERE feed_id='test' ORDER BY rowid LIMIT 3)"`, and start it again. Within a minute exactly those 3 items should appear in `#ai-ml`, batched in one message, and not again on the next poll. Run `/feed remove id:test`.

- [ ] **Step 7: Check restart behaviour and backup**

Restart the bot and press **Enter code** or **Verify** on the old panel: the buttons still work. Run `.venv/bin/python -c "from bot.db import connect; from bot.backup import backup_database; from pathlib import Path; print(backup_database(connect('data/bot.sqlite3'), Path('data/backups')))"` and confirm a `bot-YYYYMMDD.sqlite3` file with mode `-rw-------`.

- [ ] **Step 8: Deploy to the home server and repeat the feed check there**

Copy the project to the server, create `.env` and `config.toml` with the real IDs and `poll_interval_minutes = 30`, run `python -m scripts.check_feeds` on the server itself (this is the Xplore reachability test from the spec), then `mkdir -p data && docker compose up -d --build`. If any Xplore line fails with HTTP 418 or 403 from the server, report it rather than working around it.

- [ ] **Step 9: Commit any fixes**

```bash
git add -A
git commit -m "fix: issues found in end-to-end test"   # only if there were any
```
