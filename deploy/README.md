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
  meeting `done`. Since most of the call was transcribed while it went on, the wait after `/record stop` is mostly
  the summary.

If `/record stop` happens while a live run is busy, that run finishes its pass and exits; the next tick (2 minutes
after it ends) sees the meeting `queued` and starts the full job. The timer uses `OnUnitInactiveSec`, counted from
the end of each run, so a long run delays the next tick instead of overlapping it; the worker's flock in the
meetings folder and the fixed container name also prevent overlap.

Files:

| File | What it is |
| --- | --- |
| `Dockerfile.worker` | The job image (faster-whisper, requests, `worker/`, `bot/recording/meeting.py`), uid 1000, runs under `nice -n 19 ionice -c 3` |
| `docker-compose.yml` | `worker` and `ollama` services (profile `job`), CPU/memory limits, model volumes; the meetings bind mount |
| `deploy/ieee-meeting-worker.timer` | Starts the service 1 minute after boot, then 2 minutes after each run ends |
| `deploy/ieee-meeting-worker.service` | Oneshot: skips unless a meeting is pending, runs the job, then stops Ollama |
| `deploy/meeting-pending.sh` | The cheap check: exit 0 if a manifest has status `recording`, `queued`, `transcribing` or `summarizing` |
| `deploy/meeting-job.sh` | The run itself: the full job (with Ollama) if a meeting has ended, otherwise a live transcription run |

## Why a timer and not a path unit

A `.path` unit on `data/meetings` only fires when an entry directly in that folder changes (a new meeting folder).
`manifest.json` lives one level down, in `data/meetings/<id>/`, and is rewritten in place (atomic rename), so a
directory watch never sees the `recording` -> `queued` transition. inotify is not recursive and systemd cannot
watch a glob. The timer avoids that: each tick runs `meeting-pending.sh` as `ExecCondition` (one `grep`, no
container), so idle cost is negligible, and a queued meeting starts within about 2 minutes. While a call is still
`recording`, the live run transcribes finished segments every few minutes, so transcription overlaps with the call
as the design intends.

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
```

To reprocess a meeting, set its `status` in `data/meetings/<id>/manifest.json` back to `queued` (segments that
already have a `.txt` are not transcribed again). To pause processing: `sudo systemctl stop ieee-meeting-worker.timer`.

## Limits

| Container | CPU | Memory (no swap) | Priority |
| --- | --- | --- | --- |
| `worker` | 6 of 8 threads, weight 128 (default 1024) | 6 GB | `nice 19`, idle I/O |
| `ollama` | 6 of 8 threads, weight 128 | 12 GB | `nice 19`, idle I/O |

During a call only `worker` runs (Ollama is not started). The two do their heavy work one after the other
(transcription, then summarizing), and `OLLAMA_KEEP_ALIVE=0`
unloads Gemma right after each request. Memory limits are ceilings: a run that exceeds one is killed (the unit then
shows as failed and the timer retries 2 minutes later) instead of pushing the bot and other services into swap.
The service times out after 8 hours; `ExecStopPost` always removes the job container and stops Ollama.

`docker compose down -v` deletes the model volumes as well as `bot-data`; never add `-v` unless you mean it.
