# Meeting processing job: install on the server

Each meeting lives in `data/meetings/<id>/manifest.json`. A systemd timer on the host checks every 2 minutes for a
meeting that needs work and, only if there is one, runs the one-shot `worker` container. Both `worker` and
`ollama` are profile-gated (`job`), so `docker compose up` never starts them, and nothing stays running between
runs. The bot gets no Docker access.

`deploy/meeting-job.sh` picks one of two runs:

- **During the call** (a meeting is `recording`, none has ended): `worker python -m worker.run --live` with
  `--no-deps`. It transcribes the segments the bot has closed so far and exits; it never summarizes, never writes
  the manifest (the bot owns it until `/record stop`) and does not start Ollama. A tick with no new segments exits
  in about a second without loading Whisper. A tick with new segments loads Whisper fresh (a few seconds from the
  model cache, 3-4 GB of RAM while it runs): simpler than a long-lived worker, and the cost is only paid when there
  is speech to transcribe.
- **After the call** (a meeting is `queued`, or `transcribing`/`summarizing` after a crash): the full job, which
  starts Ollama through `depends_on`. It transcribes what is left, frees Whisper, summarizes, then marks the
  meeting `done`. When the container exits (done, failed or killed by its memory limit), `meeting-job.sh` stops
  Ollama again.

The summary is made only after the call, in fixed windows of `CHUNK_MINUTES` (12) minutes of the meeting: window k
holds what was said from minute 12k to 12(k+1). Each window gets its notes (topics, decisions, tasks, open
questions) from Gemma, saved in `data/meetings/<id>/notes/12min/<k>.md` as soon as they are made, and a final merge
pass turns all notes into `summary.md`. Notes already on disk are reused, so a job killed during the long summary
(a timeout, a reboot, the memory limit) or a retry only makes the windows still missing.

If `/record stop` happens while a live run is busy, that run finishes its pass and exits; the next tick (2 minutes
after it ends) sees the meeting `queued` and starts the full job. The timer uses `OnUnitInactiveSec`, counted from
the end of each run, so a long run delays the next tick instead of overlapping it; the worker's flock in the
meetings folder and the fixed container name also prevent overlap.

### How long after `/record stop`

Rough numbers for the server's CPU (Whisper turbo about 1.2 s per second of speech; Gemma 3 12B reads about 7
tokens/s and writes about 2.7 tokens/s), for a 1-hour meeting:

- transcription runs during the call and leaves a tail of about 10-15 minutes after it ends;
- the summary takes about 53 minutes: five 12-minute windows at about 10 minutes each, plus the merge pass;
- so the summary is posted about **1 hour 5 minutes** after `/record stop`.

These are estimates; time a real meeting with `journalctl -u ieee-meeting-worker`.

### Why not summarize during the call

Making the window notes during the call (a second container with Ollama, started between transcription runs) was
built and evaluated, and rejected. The gain is small: the CPU is shared, and when people talk most of the time
Whisper already uses most of it during the call, so few windows would get their notes before the end. The cost is
not: a second container per tick, an exit-code handshake between the two runs, rules for when a window is
complete while audio is still arriving, and Ollama (up to 12 GB) running mid-call, against the rule that other
services on the server come first. To bring it back would need spare CPU during calls (a faster server, or a
smaller or faster Whisper), a memory budget for Ollama next to the bot and the other services during the call, and
the window-completeness check (a window is complete only when no segment still being written can start in it).
The per-window notes files already exist, so a run during the call would only have to write them; the summary
after the call already skips any window that has notes.

### Window size and the model's context

Gemma runs with an 8192-token context that holds the prompt and the answer. A window is sent with about 400
tokens of instructions and 1500 tokens are kept for the answer, so at most 5500 tokens of transcript go in one call.
The worker estimates tokens on the high side (2.5 characters per token, every digit one token: about 2.5 tokens
per Greek word against the 2.2 measured) and splits a window that would not fit into parts, whose notes go into
the same file. The merge pass is guarded the same way: notes that do not fit one call are merged in groups, then
the group summaries are merged, so a 3-hour meeting still fits. Greek speech is about 120-150 words a minute, so
12 minutes is about 3,400 tokens at a typical pace and up to about 5,000 when someone talks non-stop (plus speaker
labels, which carry 18-digit Discord ids for speakers with no name yet). 20-minute windows do not fit: typical pace
is already about 5,700 tokens and a dense one 8,000 or more, so they would be split anyway. `CHUNK_MINUTES` can be
changed in `docker-compose.yml`; notes are kept per window size, so notes made with another size are ignored rather
than mixed in.

Files:

| File | What it is |
| --- | --- |
| `Dockerfile.worker` | The job image (faster-whisper, requests, `worker/`, `bot/recording/meeting.py`), uid 1000, runs under `nice -n 19 ionice -c 3` |
| `docker-compose.yml` | `worker` and `ollama` services (profile `job`), CPU/memory limits, model volumes; the meetings bind mount |
| `deploy/ieee-meeting-worker.timer` | Starts the service 1 minute after boot, then 2 minutes after each run ends |
| `deploy/ieee-meeting-worker.service` | Oneshot: skips unless a meeting is pending, then runs `meeting-job.sh` |
| `deploy/meeting-pending.sh` | The cheap check: exit 0 if a manifest has status `recording`, `queued`, `transcribing` or `summarizing` |
| `deploy/meeting-job.sh` | The run itself: the full job (with Ollama, stopped again afterwards) if a meeting has ended, otherwise a live transcription run without Ollama. Also the way to run the job by hand |

## Why a timer and not a path unit

A `.path` unit on `data/meetings` only fires when an entry directly in that folder changes (a new meeting folder).
`manifest.json` lives one level down, in `data/meetings/<id>/`, and is rewritten in place (atomic rename), so a
directory watch never sees the `recording` -> `queued` transition. inotify is not recursive and systemd cannot
watch a glob. The timer avoids that: each tick runs `meeting-pending.sh` as `ExecCondition` (one `grep`, no
container), so idle cost is negligible, and a queued meeting starts within about 2 minutes. While a call is still
`recording`, the live run transcribes finished segments every few minutes, so transcription overlaps with the call
as the design intends; the summary waits for the end of the call.

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

Run a pending meeting now, without waiting for the timer (the same run as a tick, skipped if nothing is pending,
and never a second one next to a run already going):

```bash
sudo systemctl start ieee-meeting-worker.service
journalctl -u ieee-meeting-worker -f              # to watch it
```

To run the job in the foreground instead (for example to debug it), stop the timer first and wait for a run in
progress to finish, then use the same script the service runs, which also stops Ollama afterwards:

```bash
sudo systemctl stop ieee-meeting-worker.timer
systemctl is-active ieee-meeting-worker.service   # wait until this says "inactive"
sh deploy/meeting-job.sh data/meetings            # Ctrl+C removes the container and stops Ollama
sudo systemctl start ieee-meeting-worker.timer
```

Do not use a bare `docker compose --profile job run worker` or `docker compose --profile job stop ollama` while the
timer is enabled: the stop would kill the Ollama of a run the timer started, and a run started by the timer meanwhile
cannot tell your container is still using Ollama. If a run was killed hard (Ollama still listed by
`docker compose --profile job ps`), the next full run stops it; to stop it sooner, stop the timer as above, then
`docker compose --profile job stop ollama`.

To reprocess a meeting: stop the timer as above, set `"status"` in `data/meetings/<id>/manifest.json` back to
`queued`, and handle the bot's posted marker as described in the bot section of the [main README](../README.md) so
the new summary is posted; then start the timer again. Segments that already have a `.txt` are not transcribed again
and windows that have a notes file are not extracted again; to make the summary from scratch, delete the meeting's
`notes/` folder as well. The check reads the manifest with `grep`, so keep `"status": "<value>"` a plain JSON string
(any spacing and indentation is fine).

To pause processing: `sudo systemctl stop ieee-meeting-worker.timer` (a run in progress finishes;
`sudo systemctl stop ieee-meeting-worker.service` cancels it, removing the container and stopping Ollama).

## Limits

| Container | CPU | Memory (no swap) | Priority |
| --- | --- | --- | --- |
| `worker` | 6 of 8 threads, weight 128 (default 1024) | 5 GB | `nice 19`, idle I/O |
| `ollama` (`ollama/ollama:0.40.12`) | 6 of 8 threads, weight 128 | 12 GB | `nice 19`, idle I/O (nice only if the image has no `ionice`) |

The limits add up to 17 GB, more than the 16 GB box (about 12 GB free), and that is safe only because the two never
hold a model at the same time; the limits are ceilings against a runaway, not reservations:

- During a call only `worker` runs, with `--no-deps`: Ollama is not started.
- In the full job both containers are up, but Ollama holds no model until the first summary request: models load on
  demand and `OLLAMA_KEEP_ALIVE=0` unloads Gemma right after each request, so idle Ollama is a few hundred MB at most.
  The worker transcribes first, drops the Whisper model and runs `gc.collect()` (`worker/run.py`, `_transcribe`),
  and only then waits for Ollama and sends the first request. While Gemma runs (about 8 GB plus the 8192-token
  context), the worker is a Python process waiting on HTTP. A second meeting in the same job is transcribed only
  after the previous summary's last request has returned, when Gemma is being unloaded.
- So the peak is about 4 GB while transcribing and about 9-10 GB while summarizing, never both.

The worker limit (5 GB) leaves about 1 GB above Whisper turbo int8's 3-4 GB. Ollama's 12 GB is kept: the model file
alone is about 8 GB, Ollama maps it into memory (counted against the limit), and a limit too tight would kill or
thrash the summary rather than protect anything. A run that exceeds a limit is killed (the unit then shows as
failed and the timer retries 2 minutes later) instead of pushing the bot and other services into swap.

The service times out after 8 hours. On a timeout or a `systemctl stop`, systemd sends SIGTERM to `meeting-job.sh`
only (`KillMode=mixed`), which removes the job container and stops Ollama. The unit has no `ExecStopPost`, because
systemd runs it after a skipped tick too and an idle tick must stay a single `grep`. Exit code 75 (another worker
holds the lock) counts as success. After a full run whose worker exits 75, Ollama is left running only if it was
already running before the run started (it belongs to the other worker).

`docker compose down -v` deletes the model volumes as well as `bot-data`; never add `-v` unless you mean it.
