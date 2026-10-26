# Meeting recorder: handoff and status

Written 2026-10-26 to continue the work from another computer. Read this first, then the design in
`docs/superpowers/specs/2026-10-25-meeting-recorder-design.md`. Branch: `feature/meeting-recorder`.

## The goal

Let officers record a Discord voice meeting with `/record start` and `/record stop`. Afterwards the bot posts a
Greek transcript (as a `.txt`) and a structured Greek summary in a summaries channel. The owner's constraints:

- **No paid service.** Everything runs locally on the branch's own server.
- **Greek only.** All meetings are in Greek.
- **Quality over speed.** A summary 1-2 hours after the meeting is fine, as long as it is good.
- **Nothing running when idle.** Meetings happen about twice a month, so no resident worker, model or daemon.
- **Other services come first.** Processing must run at the lowest priority and never starve the server.

## The server

Arch Linux box `ieee-sb-tuc`, user `docker`: Intel i7-3770 (4 cores / 8 threads, AVX but **no AVX2**), 16 GB RAM
(about 12 GB free), GTX 950 (2 GB VRAM, not used), 1 Gbps. The bot runs in Docker (`docker-compose.yml`), data in a
Docker-managed volume `bot-data` mounted at `/app/data`.

## Decisions made (do not re-litigate)

| Topic | Decision |
| --- | --- |
| Consent | The announcement in the voice channel's own text chat is enough; no opt-in step |
| Who can record | Officers only (the existing `officer_only()` check, Officer role from `/setup roles`) |
| Concurrency | Only one recording or processing run can exist at a time |
| Summaries channel | Set by officers like the bot's other channels (`/channel set`) |
| Retention | Transcripts and summaries stay until deleted manually; audio is deleted after transcription |
| Job launcher | A **systemd path unit on the host** starts a one-shot `docker compose run` when a meeting is queued. The bot gets no Docker socket |
| Transcription | `faster-whisper` `large-v3-turbo`, int8, CPU, 8 threads, beam 1, Greek, silence filter, vocabulary prompt |
| Summary | **Gemma 3 12B** through Ollama (model name is a config value) |
| Recording | `discord-ext-voice-recv` plus our own DAVE decryption (see below); per-speaker segments |

## Benchmarks (run on the server, 2026-10-25)

Whisper, 25-second synthetic Greek clip (text-to-speech), so real recordings are still untested:

| Test | Speed | WER |
| --- | --- | --- |
| `large-v3`, 4 threads, beam 5 | 3.5x realtime | 8.2% |
| `large-v3-turbo`, 4 threads, beam 5 | 1.8x realtime | 9.6% |
| `large-v3-turbo`, 8 threads, beam 1, vocabulary prompt | **1.2x realtime** | **4.1%** |

Summary models on a built-in 3-minute Greek meeting with traps (a corrected deadline, an undecided topic, a postponed
topic):

| Model | Read / write tokens per s | Est. 1-hour meeting | Result |
| --- | --- | --- | --- |
| Gemma 3 12B | 7.2 / 2.4 | 53 min | 9/9 facts. Added one inferred task nobody was given |
| Gemma 3 4B | 23.4 / 7.4 | 17 min | Dropped the whole budget, invented a date and a status |
| Llama-Krikri-8B | 10.9 / 4.0 | 34 min | Natural Greek but tasks under "decisions", missed facts |

Expected total after a 1-hour meeting ends: about 10-15 minutes of leftover transcription plus about 53 minutes of
summarizing, roughly 1 hour 5 minutes. These are estimates extrapolated from short clips. The summary prompt must say
"list only tasks that were explicitly assigned".

Re-run them (everything runs in throwaway containers and is removed afterwards, needs about 16 GB free disk):

```bash
scripts/run_benchmark.sh --whisper large-v3-turbo --threads 8 --beam 1   # Whisper + Gemma
scripts/run_summary_benchmark.sh                                         # the three summary models
TRANSCRIPT=/path/real.txt scripts/run_summary_benchmark.sh               # on a real transcript
AUDIO=/path/call.wav scripts/run_benchmark.sh                            # on a real recording
```

## The key technical finding: DAVE

Discord encrypts voice end to end (DAVE). discord.py 2.7 negotiates it when the bot joins (through the `davey`
package), but `discord-ext-voice-recv` 0.5.2a179 only removes the transport encryption, so received frames are still
encrypted and opus decoding fails ("corrupted stream"). Upstream has open pull requests and issues about it
(imayhaveborkedit/discord-ext-voice-recv: PR 54, 56, 58, 62, 65; issues 53, 61, 64), none merged when checked.

Our approach, in `bot/recording/`: the sink asks the library for raw opus (`wants_opus()` is true), `DaveDecryptor`
removes the DAVE layer with `davey`'s session that discord.py already holds (`voice_client._connection.dave_session`,
`decrypt(user_id, MediaType.audio, payload)`), then we decode with libopus ourselves. It uses private attributes
(`_connection`, `_get_id_from_ssrc`), so it is the likeliest thing to break after a library update. **It has only been
tested with fakes. A live call has not been tried yet.** If it cannot be made to work, the fallback is a small separate
recorder (for example Node with `@discordjs/voice`), see the design's risk table.

## State of the branch

Built, with tests (309 pass in total: the 261 that existed plus 48 new ones):

- `bot/recording/pcm.py`: 48 kHz stereo to 16 kHz mono.
- `bot/recording/segments.py`: per-speaker WAV segments split at pauses over 2 s or at 5 min; thread-safe.
- `bot/recording/dave.py`: DAVE decryption with statistics.
- `bot/recording/pipeline.py`: decrypt, decode, convert, write, one opus decoder per speaker.
- `bot/recording/sink.py`: the `voice_recv.AudioSink` glue.
- `bot/recording/meeting.py`: `manifest.json` per meeting, "one at a time" rule, restart recovery; thread-safe.
- `bot/cogs/recorder.py`: `/record start` and `/record stop` (officers only; announcement before any audio; refuses
  if the announcement cannot be posted; stop reports captured segments and decrypt statistics). Registered in
  `bot/main.py`.
- `bot/config.py`: new `meetings_dir` setting (default `data/meetings`); `config.example.toml` updated.
- `requirements.txt`: `discord.py[voice]>=2.7,<3` and `discord-ext-voice-recv==0.5.2a179`. `Dockerfile`: `libopus0`.
  The image was built and checked: opus loads, `davey` is present, a decode round trip gives the expected sizes.
- `scripts/benchmark_models.py`, `benchmark_summary.py`, `benchmark.compose.yml`, `run_benchmark.sh`,
  `run_summary_benchmark.sh`: the benchmarks above.

Known limits of the spike: after `/record stop` the meeting status is `done` (it becomes `queued` once the processing
job exists, see `STATUS_AFTER_STOP` in `bot/cogs/recorder.py`); nothing handles the bot being kicked from the call or
the channel emptying; there is no `/record status` yet.

## Dev environment

Use Python 3.12 to match the Dockerfile. Python 3.13 removed `audioop`, which the voice library imports.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q          # 309 passed
```

## Next steps, in order

1. **Live test the spike** (this is the gate for everything else). Deploy the branch (a separate test bot token is
   better, since deploying restarts the production bot), join a voice channel with one or two consenting people,
   `/record start`, talk for a minute, `/record stop`. The report should show segments and a nonzero decrypted count.
   Listen to a file: `docker compose exec bot ls -R /app/data/meetings`, then
   `docker compose cp bot:/app/data/meetings/<id>/<speaker>/000001.wav .`. If it fails, check the decrypt statistics
   in the report and the bot log, and consider the fallback recorder.
2. **Transcription job.** A `worker` image (Python, `faster-whisper`, `ffmpeg` only if needed) that reads a meeting's
   `manifest.json`, transcribes each segment with the settings above, merges speakers by `start` offset into lines
   `[hh:mm:ss] Name: text`, skips segments that already have a transcript (resume after a crash), and deletes audio
   after each transcript. Start it when recording starts so it overlaps with the call. Test on a real Greek recording.
3. **Summary stage.** Split the transcript into 10-15 minute pieces, extract topics, decisions, tasks and open
   questions from each with Gemma 3 12B (Ollama started for this step only, `keep_alive` 0, 8192 context, temperature
   0.2), then a merge pass. Retry an Ollama error once (one unexplained HTTP 500 happened in a benchmark). Greek
   headings: Θέματα, Αποφάσεις, Εργασίες, Ανοιχτά θέματα.
4. **Launching and limits.** The systemd path unit and its oneshot service, container CPU and memory limits, `nice 19`,
   logging. `data` is a Docker-managed volume, so the host cannot see it by default: either watch
   `/var/lib/docker/volumes/<project>_bot-data/_data/meetings` or make `meetings_dir` a bind mount. Write both unit
   files and the install steps in the README.
5. **Posting.** The bot sees status `done` and posts the summary plus the transcript `.txt` in the summaries channel
   (add a reserved topic for it to `bot/channels.py` so `/channel set` can pick it), with a status message that is
   edited as processing advances. Change `STATUS_AFTER_STOP` to `queued`.
6. **Polish.** `/record status`, handling a kick or an empty channel, README and `config.example.toml` docs.

## Conventions in this repo

Design docs go in `docs/superpowers/specs/`, plans in `docs/superpowers/plans/`. Tests are in `tests/`, written test
first; new Discord commands follow `bot/cogs/officer.py` (guild-only, `@officer_only()`, `audit(...)` for officer
actions) and are gated to the configured server automatically by `GuildLockedTree`. Commit messages are short and
lowercase.
