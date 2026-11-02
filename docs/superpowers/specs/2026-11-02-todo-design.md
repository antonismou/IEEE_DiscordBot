# Officer To-Do and Reminders: Design

Date: 2026-11-02. Scope: phases 1 and 2 only (see "Non-goals").

## Intent

Branch officers lose track of who is doing what and by when. This adds a shared task list and reminders inside Discord, so nothing needs a separate app. It is **officers only**: members never see it, can't use it, and none of their data is involved.

## Goals

1. Officers can create, list, edit, assign, complete and delete tasks.
2. **Every task is shared with the whole officer team.** A task can be assigned to one officer, but everyone sees it, so the team can remind each other. Every new, assigned, completed or deleted task is announced in the officers' tasks channel.
3. Tasks with a due date remind automatically: **2 days before, 1 day before, 1 hour before, at the due time, and 1 day overdue** (if still open).
4. Reminders can arrive by **DM**, in a **channel**, or **both**.
5. Officers can set free-standing reminders (`/todo remind`) not tied to a task.
6. Reminder DMs carry buttons: **Done**, **Snooze 1h**, **Snooze until tomorrow**.
7. Tasks can repeat (daily / weekly / monthly).
8. A **pinned board** message in a channel that the bot keeps up to date with open team tasks.
9. The commands are **hidden from non-officers** as far as Discord allows (see "Visibility").

## Non-goals (not in this build)

Personal/private tasks (everything is shared), statistics, escalation pings, projects/tags, subtasks and comments, creating tasks from messages, tasks from meeting transcripts, member-facing use. Statuses beyond open/done (no "in progress" or "blocked").

## Anti-goals

- A non-officer ever seeing, running, or being shown a task, reminder or the board.
- A task or reminder visible outside the officers: the tasks channels must be private, and the bot warns when they are not.
- A reminder sent twice, or a restart replaying old reminders as a burst.
- Reminder text pinging `@everyone` or arbitrary roles/users.

## Commands

All are subcommands of one group, `/todo`, so there is a single place to control visibility. Every response is ephemeral except what is posted to channels by reminders and the board.

| Command | What it does |
|---|---|
| `/todo add title [due] [assignee] [priority] [repeat] [notify] [description]` | Create a task, announced in the channel. `notify` (how reminders arrive): `dm`, `channel`, `both` (default `both`). `priority`: low / normal / high. `repeat`: none / daily / weekly / monthly. |
| `/todo list [view]` | Paged list. `view`: `mine` (default: assigned to me), `all` open tasks, `unassigned`, `overdue`, `done` (last 20). |
| `/todo done task` | Complete a task (for repeating tasks, also creates the next one). |
| `/todo edit task [title] [due] [priority] [repeat] [notify] [description]` | Change fields. `due: none` clears the date. |
| `/todo assign task user` | Assign a task to an officer, or `none` to unassign. |
| `/todo delete task` | Delete a task and its pending reminders. |
| `/todo remind when text [delivery]` | A free-standing reminder to myself. `delivery`: `dm`, `channel`, `both` (default `both`, so the team sees it too). |
| `/todo reminders` | Pending reminders for the team (shared), with cancel buttons for my own, with cancel buttons. |

`task` arguments use autocomplete that lists only tasks the caller may see, as `#12 Title (due fri 18:00)`.

### Who may touch what

- Only officers can use any `/todo` command, and every officer can view, edit, complete, assign and delete **any** task (it is a team list). An admin counts as an officer (existing `is_officer` rule).
- Free-standing reminders are shown to everyone but only their owner can cancel them.
- Non-officers cannot use commands, press buttons, or see the channels (see "Visibility").

### Dates

Parsed in **Europe/Athens** by our own small parser (no new dependency). Accepted:

- `in 30m`, `in 3h`, `in 2d`, `in 1w`
- `today 18:00`, `tomorrow`, `fri`, `friday 09:30` (a weekday name means the next such day strictly after today)
- `2026-11-20`, `2026-11-20 14:00`, `20/11`, `20/11/2026 14:00` (day first, as in Greece)
- Date with no time defaults to **18:00** (my call).
- Must be in the future and within two years; otherwise a friendly error. Times are shown to users as Discord timestamps (`<t:…:f>`), so they render in each person's own zone.

## Reminders

### Schedule for a task

When a task gets a due date (on add, edit or repeat spawn), reminder rows are created for: `due − 2d`, `due − 1d`, `due − 1h`, `due`, `due + 1d` (kind `overdue`). Rows whose time is already past are not created. Completing, deleting, reassigning or changing the due date removes unsent rows (reassign and due change then recreate them).

### Who gets it and where

| Task | DM | Channel (`todo-reminders`) |
|---|---|---|
| Assigned | the assignee | yes, mentioning the assignee |
| Unassigned | nobody | yes, mentioning the Officer role |
| Free-standing reminder | the owner | yes, mentioning the owner |

`notify` / `delivery` (`dm`/`channel`/`both`) selects among these; default is `both`, so the channel always shows what is coming up, which is how the team reminds each other. If a DM fails (closed DMs) and a channel is set, the reminder still appears in the channel (and if `dm` was the only choice, it is posted there as a fallback). If no channel is bound, channel delivery is skipped and the DM still goes out. If neither can be delivered, a line without the task text is sent to officer-log.

### Announcements

Besides reminders, `todo-reminders` gets a short line when a task is **created**, **assigned**, **completed** (including who did it and, for repeating tasks, the next due date) or **deleted**, e.g. "📝 Task #12 *Book the venue* created by @a, assigned to @b, due <t:…:f>". No pings in announcements except the assignee on create/assign.

Allowed mentions are limited to exactly the one user or the Officer role the reminder intends.

### Delivery semantics

- A loop runs every 30 s and selects rows with `fire_at <= now AND sent_at IS NULL`.
- **At most once:** the row is marked sent *before* sending, so a crash can lose a reminder but never repeats one (my call).
- **Catch-up after downtime:** a missed `2d/1d/1h` reminder whose task is already past due is dropped (the `due`/`overdue` ones supersede it). Everything else is sent late, with a "(late)" suffix if more than 10 minutes behind.
- **Role check at send time:** if the target is no longer an officer or has left the server, their reminders are dropped and marked sent.
- Reminder DMs and channel reminders include buttons (below); the buttons survive bot restarts.

### Buttons

Persistent `discord.ui.DynamicItem` buttons with custom ids `todo:done:<task>`, `todo:snooze1h:<reminder>`, `todo:snoozetmr:<reminder>`. Because a click in DMs has no member, the handler re-checks officer status by fetching the member from the configured guild, (any officer may press Done or Snooze on any task). A snooze creates a new `snooze` reminder row; it does not alter the task's own schedule.

### Repeating tasks

Completing a repeating task creates the next open copy: due date advanced by one day/week/month from the **previous due date** (not from now), repeated until it is in the future. Monthly keeps the day of the month and clamps to the month's last day when it doesn't exist (31 Jan → 28 Feb), then continues from the clamped date (→ 28 Mar); the drift is accepted for simplicity (my call). A repeating task without a due date is rejected at creation.

## Pinned board

Bind a channel with `/channel set todo-board #channel` (it may be the same channel as `todo-reminders`). The bot posts one embed there and edits it (never reposts) when any task changes. Sections: Overdue, Due in the next 7 days, Later, No date. Each line: `#id title · @assignee · <t:due:R>`. At most ~25 lines shown, then "+N more". The message id is kept in `server_settings`. If the message was deleted, the bot posts a fresh one. Refreshes are debounced: changes set a flag, the 30 s loop does the edit.

## Visibility

Discord cannot hide a command per *role* from code. What we do:

1. The `/todo` group is created with `default_permissions=Permissions(administrator=True)`, so by default it appears only for administrators.
2. A server admin does a one-time step: **Server Settings → Integrations → (this bot) → /todo → add the Officer role**. After that officers see it and everyone else doesn't. This is documented in the README and mentioned in the `/setup roles` reply.
3. Independently of what Discord shows, every subcommand carries `@officer_only()`, so a visible-but-not-allowed attempt is refused with the standard officer-only message. Visibility is a convenience; the check is the security.

The channels (`todo-reminders`, `todo-board`) are added to the private topics so `/channel set` warns if @everyone can see them. Because every task title is posted there, the channels **must** be officers-only.

## Data

New tables (created via `SCHEMA` in `bot/db.py`, `CREATE TABLE IF NOT EXISTS`, like the rest):

```
tasks(id PK AUTOINCREMENT, title, description,
      created_by, assignee_id NULL, due_at NULL, priority, status CHECK('open','done'),
      repeat, notify, created_at, done_at NULL, done_by NULL)
reminders(id PK AUTOINCREMENT, task_id NULL, user_id, kind, fire_at, text NULL,
          delivery, sent_at NULL)
INDEX reminders(sent_at, fire_at)
```

Timestamps are ISO UTC strings via `to_iso`/`from_iso`. Limits: title 100 characters, description 500, 500 open tasks, 25 pending free-standing reminders per officer.

- **Backups:** covered by the existing nightly database backup.
- **Deletion:** `/forget-me` and `/member delete` call `todo.delete_by_user(user_id)`: delete their free-standing reminders, unassign them from tasks, and replace them as creator/completer by `0`. Task titles they wrote stay (branch work, not personal data), mirroring how quotes treat `quoted_by`. Messages already posted in Discord channels are not touched.
- **Audit log:** one line per action using ids only, e.g. "created task #12", "assigned #12 to @x", "deleted task #12". Titles are not written to the audit log (they are already in the private tasks channel).

## Constraints

- Follow repo style: `from __future__ import annotations`, repos take `conn`, blocking work none (SQLite only), cogs thin and logic in `bot/todo/`.
- No new dependencies.
- Existing gate (`GuildLockedTree`) and error handler apply unchanged.
- Time source is injectable (`now` parameter) so tests never sleep.

## Left to the builder

Embed styling and exact wording; button labels; paging size (10 per page suggested); the task-id autocomplete limit (25, Discord's cap).

## My calls to check

Default time 18:00 for date-only input; reminder offsets include `due` and `overdue +1d` beyond the 2d/1d/1h you asked for; announcements for create/assign/done/delete in the channel; default delivery `both`; at-most-once delivery; repeats advance from the previous due date; repeating tasks require a due date; only open/done statuses; single `/todo` group including `remind`; any officer can edit any task.
