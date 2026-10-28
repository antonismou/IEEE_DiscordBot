# Meeting processing job: install on the server

Each meeting lives in `data/meetings/<id>/manifest.json`. A systemd timer on the host checks every 2 minutes for a
meeting that needs work and, only if there is one, runs the one-shot `worker` container. Both `worker` and
`ollama` are profile-gated (`job`), so `docker compose up` never starts them, and nothing stays running between
runs. The bot gets no Docker access.

The summary is made in fixed windows of `CHUNK_MINUTES` (12) minutes of the meeting: window k holds what was said
from minute 12k to 12(k+1). Each window gets its notes (topics, decisions, tasks, open questions) from Gemma, kept
in `data/meetings/<id>/notes/12min/<k>.md`, and a final merge pass turns all notes into `summary.md`. Most of the
notes are made **during the call**, so the wait after `/record stop` is shorter.

`deploy/meeting-job.sh` picks the runs:

- **During the call** (a meeting is `recording`, none has ended): first `worker python -m worker.run --live` with
  `--no-deps`. It transcribes the segments the bot has closed so far; it never summarizes, never writes the
  manifest (the bot owns it until `/record stop`) and does not start Ollama. A tick with no new segments exits in
  about a second without loading Whisper; one with new segments loads Whisper fresh (a few seconds from the model
  cache, 3-4 GB of RAM while it runs). Then, **only if** a window is complete and has no notes yet, the run exits
  with code 3 and the script starts a second container, `worker python -m worker.run --extract`, which starts
  Ollama through `depends_on`, makes the notes of the complete windows without loading Whisper, and exits; the
  unit's `ExecStopPost` then stops Ollama. Two containers keep the models apart for certain: Whisper's memory is
  gone with its process before Ollama starts (worker limit 6 GB + Ollama 12 GB would not fit next to the other
  services on the 16 GB server, and both want all the CPU). The decision is the worker's, not the shell's,
  because it needs the manifest, the segment texts and the clock. A window counts as complete when the meeting has
  run 6 minutes past its end (a segment is closed at 5 minutes of audio; 1 minute of margin), every segment that
  starts before its end is transcribed, and no audio file still being written can belong to it.
- **After the call** (a meeting is `queued`, or `transcribing`/`summarizing` after a crash): the full job, which
  starts Ollama through `depends_on`. It transcribes what is left, frees Whisper, makes the notes of the windows
  still missing (the last, partial one included), merges, then marks the meeting `done`. Notes already on disk are
  reused, so a crash, a retry or a meeting cut short by a bot restart only makes the missing ones.

A failed notes run during the call (Ollama down, two errors in a row) only logs: the meeting stays `recording` and
the window is tried again on the next tick or after the call. If `/record stop` happens while a live run is busy,
that run finishes the segment or window it is on (notes made are kept) and exits; the next tick (2 minutes after it
ends) sees the meeting `queued` and starts the full job. The timer uses `OnUnitInactiveSec`, counted from the end
of each run, so a long run (a notes run during the call can take 10-30 minutes) delays the next tick instead of
overlapping it; the worker's flock in the meetings folder and the fixed container name also prevent overlap.

### How long after `/record stop`

Rough numbers for the server's CPU (Whisper turbo about 1.2 s per second of speech; Gemma 3 12B reads about 7
tokens/s and writes about 2.7 tokens/s): one 12-minute window takes about 10-13 minutes to extract, and its speech
takes up to about 14 minutes to transcribe. So a busy call produces more work than the CPU can do while it lasts,
and notes during the call use only the time Whisper leaves free (silence, short turns). For a 1-hour meeting:

| Meeting | Notes made during the call | Wait after `/record stop` |
| --- | --- | --- |
| Before this change | none | about 1-1.5 hours |
| Talk most of the time | 0-1 of 5 windows | about 1-1.25 hours |
| Typical, with pauses (about half the time someone speaks) | 2-3 of 5 | about 40-60 minutes |

The floor is the tail: the last window (up to 12 minutes of speech), the one before it if it ended in the last 6
minutes, the last segments' transcription and the merge pass, about 20-30 minutes. These are estimates; time a real
meeting with `journalctl -u ieee-meeting-worker`.

### Window size and the model's context

Gemma runs with an 8192-token context that holds the prompt and the answer. A window is sent with about 400
tokens of instructions and 1500 tokens are kept for the answer, so at most 5500 tokens of transcript go in one call.
The worker estimates tokens on the high side (2.5 characters per token, every digit one token: about 2.5 tokens
per Greek word against the 2.2 measured) and splits a window that would not fit into parts, whose notes go into
the same file. The merge pass is guarded the same way: notes that do not fit one call are merged in groups, then
the group summaries are merged, so a 3-hour meeting still fits. Greek speech is about 120-150 words a minute, so
12 minutes is about 3,400 tokens at a typical pace and up to about 5,000 when someone talks non-stop (plus speaker
labels, which during the call carry 18-digit Discord ids). 20-minute windows do not fit: typical pace is already
about 5,700 tokens and a dense one 8,000 or more, so they would be split anyway and only make the tail after the
call longer. `CHUNK_MINUTES` can be changed in `docker-compose.yml`; notes are kept per window size, so notes made
with another size are ignored rather than mixed in.

Files:

| File | What it is |
| --- | --- |
| `Dockerfile.worker` | The job image (faster-whisper, requests, `worker/`, `bot/recording/meeting.py`), uid 1000, runs under `nice -n 19 ionice -c 3` |
| `docker-compose.yml` | `worker` and `ollama` services (profile `job`), CPU/memory limits, model volumes; the meetings bind mount |
| `deploy/ieee-meeting-worker.timer` | Starts the service 1 minute after boot, then 2 minutes after each run ends |
| `deploy/ieee-meeting-worker.service` | Oneshot: skips unless a meeting is pending, runs the job, then stops Ollama |
| `deploy/meeting-pending.sh` | The cheap check: exit 0 if a manifest has status `recording`, `queued`, `transcribing` or `summarizing` |
| `deploy/meeting-job.sh` | The run itself: the full job (with Ollama) if a meeting has ended, otherwise a live transcription run, followed by a notes run with Ollama only when a window is complete |

## Why a timer and not a path unit

A `.path` unit on `data/meetings` only fires when an entry directly in that folder changes (a new meeting folder).
`manifest.json` lives one level down, in `data/meetings/<id>/`, and is rewritten in place (atomic rename), so a
directory watch never sees the `recording` -> `queued` transition. inotify is not recursive and systemd cannot
watch a glob. The timer avoids that: each tick runs `meeting-pending.sh` as `ExecCondition` (one `grep`, no
container), so idle cost is negligible, and a queued meeting starts within about 2 minutes. While a call is still
`recording`, the live run transcribes finished segments every few minutes and makes the notes of complete windows,
so transcription and most of the summary overlap with the call.

Optional follow-up for instant start: the bot could `touch data/meetings/.queue` whenever it writes a manifest (a
3-line change in `bot/recording/meeting.py`'s `_write`). Then a path unit with `PathChanged=.../data/meetings/.queue`
and `Unit=ieee-meeting-worker.service` can replace or complement the timer. Keep the timer anyway as a safety net.

## One-time setup

All commands run in the folder that holds `docker-compose.yml` (below: `/home/docker/IEEE_DiscordBot`).

1. **Meetings folder.** Recordings, transcripts and summaries now live in a host folder bind-mounted at
   `/app/data/meetings` in both the bot and the worker; the rest of `/app/data` (database, backups) stays in the
   `bot-data` volume. Create it **before** the next `docker compose up`, owned by the containers' uid 1000:

   ```bash
   sudo install -d -m 700 -o 1000 -g 1000 data/meetings
   ```

   If it is missing, Docker creates it owned by root: the bot still starts and works, but `/record start` fails
   with a permission error until you `sudo chown 1000:1000 data/meetings`. To keep recordings made before this
   change (the spike test), copy them out of the volume **before** updating, since the bind mount hides them:
   `docker compose cp bot:/app/data/meetings/. data/meetings/ && sudo chown -R 1000:1000 data/meetings`.
   A different location can be set with `MEETINGS_HOST_DIR=/some/path` in `.env` (then use that path in the
   service's `ExecCondition` too).

2. **Update the bot** so it uses the new mount: `docker compose up -d --build`.

3. **Build the job image and pull the models once** (about 1.6 GB for Whisper and 8 GB for Gemma, kept in the
   `whisper-models` and `ollama-models` volumes):

   ```bash
   docker compose --profile job build worker
   # Whisper: download large-v3-turbo into the cache volume (no Ollama needed)
   docker compose --profile job run --rm --no-deps worker \
     python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8')"
   # Gemma: start Ollama, pull, stop
   docker compose --profile job up -d --wait ollama
   docker compose --profile job exec ollama ollama pull gemma3:12b
   docker compose --profile job stop ollama
   ```

4. **Install the units.** Adjust the paths in `ieee-meeting-worker.service` if the project is not in
   `/home/docker/IEEE_DiscordBot` (`WorkingDirectory`, and both paths in `ExecCondition` and in `ExecStart`), then:

   ```bash
   sudo cp deploy/ieee-meeting-worker.service deploy/ieee-meeting-worker.timer /etc/systemd/system/
   sudo systemd-analyze verify /etc/systemd/system/ieee-meeting-worker.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now ieee-meeting-worker.timer
   systemctl list-timers ieee-meeting-worker.timer
   ```

   Enable only the **timer**; the service has no `[Install]` section and is started by it.

## Daily use

```bash
journalctl -u ieee-meeting-worker -f              # job output (the container's stdout goes to the journal)
journalctl -u ieee-meeting-worker --since today   # skipped ticks show "condition failed", which is normal
systemctl status ieee-meeting-worker.timer        # when the next check runs
docker compose --profile job logs ollama          # Ollama's log from the last run
```

Run a pending meeting by hand, without waiting for the timer:

```bash
sudo systemctl start ieee-meeting-worker.service              # same as the timer: skipped if nothing is pending
docker compose --profile job run --rm worker                  # directly, in the foreground
docker compose --profile job stop ollama                      # afterwards, when run directly
docker compose --profile job run --rm --no-deps worker python -m worker.run --live   # a live pass, no Ollama
docker compose --profile job run --rm worker python -m worker.run --extract          # notes during a call
```

To reprocess a meeting, set its `status` in `data/meetings/<id>/manifest.json` back to `queued` (segments that
already have a `.txt` are not transcribed again, windows that have a notes file are not extracted again). To make
the summary from scratch, delete the meeting's `notes/` folder as well. To pause processing: `sudo systemctl stop ieee-meeting-worker.timer`.

## Limits

| Container | CPU | Memory (no swap) | Priority |
| --- | --- | --- | --- |
| `worker` | 6 of 8 threads, weight 128 (default 1024) | 6 GB | `nice 19`, idle I/O |
| `ollama` | 6 of 8 threads, weight 128 | 12 GB | `nice 19`, idle I/O |

The two never hold a model at the same time. During a call the transcription container runs alone, and the notes
container (with Ollama) starts only after it has exited; after the call the full job transcribes, frees Whisper,
then summarizes. `OLLAMA_KEEP_ALIVE=0` unloads Gemma right after each request, and nothing stays running between
ticks. Memory limits are ceilings: a run that exceeds one is killed (the unit then
shows as failed and the timer retries 2 minutes later) instead of pushing the bot and other services into swap.
The service times out after 8 hours; `ExecStopPost` always removes the job container and stops Ollama.

`docker compose down -v` deletes the model volumes as well as `bot-data`; never add `-v` unless you mean it.
