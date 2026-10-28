# Meeting Recorder and Summaries: Design

Date: 2026-10-25

## Overview

The bot joins a Discord voice channel on command, records each speaker separately, and posts a Greek transcript and a structured summary to a text channel. Everything runs locally on the existing server, with no paid service.

Goals:

- Officers start and stop a recording with `/record start` and `/record stop`.
- A transcript is ready roughly 10-15 minutes after a 1-hour meeting ends, and the summary within about 1-1.5 hours of the end (estimates, see Benchmark results).
- Zero cost: no external API for transcription or summarization.
- Zero idle footprint: meetings happen about twice a month, so nothing runs between them.

Non-goals (version 1):

- Live captions or real-time summaries.
- Languages other than Greek.
- Several simultaneous recordings: exactly one recording can exist at a time, server-wide.
- Keeping raw audio after processing.

## Constraints and decisions

The server's CPU decides most choices: an Intel i7-3770 (4 cores, 8 threads, AVX but no AVX2) with 16 GB RAM, about 12 GB free. The GTX 950 (2 GB VRAM) is not used.

| Area | Decision | Reason |
| --- | --- | --- |
| Hosting | Existing server, CPU only | No new cost; GPU too small to help |
| Language | Greek only | All meetings are in Greek |
| Transcription | `faster-whisper` `large-v3-turbo`, int8, 8 threads, beam 1 | 1.2x realtime and 4.1% WER in the benchmark |
| Summary | Gemma 3 12B through Ollama, model name in config | Good Greek output; swappable if a faster or better model appears |
| Voice receive | `discord-ext-voice-recv` on top of discord.py, plus our own DAVE decryption step | Avoids migrating the bot; the extension lacks DAVE support, so it must be verified in a real call |
| Processing | One short-lived container per meeting | Nothing resident between meetings |
| Audio retention | Deleted after transcription | Privacy and disk space |

## Architecture

The bot and the processing job never talk to each other directly: they share a meeting folder on disk, which also makes the job restartable.

```
1 Voice channel -> 2 Bot recorder -> 3 Meeting folder -> 7 Text channel
                                       |   ^
                          meeting queued   | reads chunks, writes transcript + summary
                                       v   |
        4 Path unit -> [ Job container: 5 Transcribe (Whisper turbo) -> 6 Summarize (Gemma 3 12B) ]
```

Speakers talk (1) and the bot writes a chunk per speaker (2) into the meeting folder (3). A systemd path unit on the host sees the queued meeting and starts the one-shot job (4), which transcribes (5) and summarizes (6) and writes the results back to the folder. The bot sees the finished status and posts the summary and transcript (7).

## Recording (bot side)

The bot only captures audio and writes it to disk; all heavy work happens in the processing job.

- **Receiving audio.** discord.py cannot receive voice on its own, so the bot adds the community extension `discord-ext-voice-recv`. It is the least disruptive option (no migration to py-cord) but is community-maintained and has broken before when Discord changed its voice protocol. A spike must prove it works on the server before anything else is built.
- **Dependencies.** `discord.py[voice]` 2.7+ (brings `PyNaCl` and `davey`), `discord-ext-voice-recv` pinned to one version, and `libopus0` in the bot image. No `ffmpeg` is needed in the bot: audio is decoded with libopus and written with Python's `wave` module.
- **End-to-end encryption (DAVE).** Discord now encrypts voice end to end. discord.py negotiates this when the bot joins, but `discord-ext-voice-recv` 0.5.2a179 only removes the transport encryption, so received audio would still be encrypted and fail to decode. Several fixes are open upstream (pull requests since March 2026) but none is merged. The bot therefore asks the library for raw opus frames, removes the DAVE layer itself with `davey`'s session (the one discord.py already keeps), and decodes with libopus. This relies on private attributes of discord.py and the extension, so it is the first thing to verify in a real call and the likeliest thing to break after a library update.
- **One stream per speaker.** Each user's audio is written to its own file, so the transcript knows who spoke without any diarization.
- **Segments, not fixed chunks.** Each speaker's continuous speech is written as one 16 kHz mono WAV file, closed after a pause of more than 2 seconds or at 5 minutes. Discord stops sending audio while someone is silent, so pauses are natural cut points, and a finished file is a ready unit of work for the job. Each segment records its speaker and its start offset from the meeting start.
- **Consent.** When recording starts the bot posts an announcement in the voice channel's own text chat. Decision: that announcement is enough, with no opt-in step.

## Processing job

A one-shot container does transcription, then summarization, then exits. Only one model is in memory at a time. A host timer starts it every 2 minutes while a meeting needs work, in one of two modes (`deploy/meeting-job.sh`):

- **During the call** (`recording`): a live run (`worker.run --live`, without Ollama) transcribes the chunks the bot has closed so far and exits. It never summarizes, never fails the meeting and never writes the manifest, which the bot owns until `/record stop`. A run with no new chunks exits without loading Whisper; otherwise Whisper is loaded fresh each run, which keeps the job stateless at the cost of a few seconds per run.
- **After the call** (`queued`): the full run starts Ollama, transcribes the remaining chunks, unloads Whisper, summarizes and marks the meeting `done`. A meeting stopped while a live run is busy is picked up by the next tick.

1. **Transcribe chunks as they appear**, during the call (the live runs above). `faster-whisper` `large-v3-turbo`, int8, CPU, 8 threads, beam size 1, language set to Greek, silence filtering on. An initial prompt lists vocabulary the transcript should get right (IEEE Student Branch, workshop, event, email, day names).
2. **Merge speakers** by timestamp into one transcript, one line per segment: `[00:00:09] Μαρία: ...`.
3. **Unload Whisper, then summarize.** The transcript is split into pieces of about 10-15 minutes. Gemma 3 12B (through Ollama, 8192-token context, temperature 0.2) extracts topics, decisions, tasks and open questions from each piece, then a final pass merges them into one summary. The prompt is in Greek and forbids adding information that is not in the text.
4. **Post and clean up.** The summary and the transcript file go to Discord, and the job removes remaining audio.

The model names are config values, so the summary model can be swapped without code changes. The job retries an Ollama error once: the benchmark hit one unexplained HTTP 500 that did not repeat.

## Commands, permissions and output

| Command | Who | Effect |
| --- | --- | --- |
| `/record start` | Officers (the Officer role chosen with `/setup roles`) | Refuses if a recording or processing run is already active. Otherwise joins the caller's voice channel, posts the announcement in its text chat, creates the meeting record, queues the processing job |
| `/record stop` | Officers | Leaves the channel, closes the last chunk, marks the meeting ended so the job can summarize |
| `/record status` (proposed) | Officers | Shows the current stage: recording, transcribing, summarizing, done |

When processing finishes, the bot posts to the summaries channel, which officers pick with `/channel set` like the bot's other channels: the summary as a message and the full transcript as a `.txt` attachment. The summary uses fixed Greek headings so every meeting looks the same:

- Θέματα (topics)
- Αποφάσεις (decisions)
- Εργασίες (tasks, with owner and deadline)
- Ανοιχτά θέματα (open questions)

A status message is posted when processing starts and edited as it advances, so officers know why the summary has not arrived yet.

## State, storage and recovery

Each meeting gets its own directory under `meetings_dir` (default `data/meetings`). Today `data` is a Docker-managed volume, so for the host's path unit to see it, step 5 must either watch the volume's path under `/var/lib/docker/volumes/` or switch this folder to a bind mount. A `manifest.json` in it is the single source of truth.

- **Manifest.** Meeting id, start time, speaker map (Discord id to display name), the list of chunks with offsets, and a status.
- **Statuses.** `recording`, `queued`, `transcribing`, `summarizing`, `done`, `failed`. (`interrupted` was written by earlier versions; the bot recovers it at startup like a `recording` one.)
- **One at a time.** `/record start` is refused while any meeting is in a status other than `done` or `failed`, so the job also never runs two meetings at once.
- **Job recovery.** The job skips any chunk that already has a transcript file, so a crash or restart resumes where it stopped.
- **Bot restart during a call.** The voice session cannot resume by itself. At startup, before it can record again, the bot ends the meeting as `/record stop` would: with at least one chunk it becomes `queued`, so the chunks already on disk are still processed, and with none it becomes `failed` ("no audio was captured"). Either way `posting.json` gets `interrupted: true`, written before the status changes, so the posted summary or failure notice says in Greek that the recording was cut short and may be incomplete. This is safe without locking: at startup the bot is the only writer of a `recording` manifest, since the job's live pass never writes it. Speaker names are saved in the manifest as each person first speaks (looked up on the event loop, never on the voice thread), and refreshed at `/record stop` without replacing a known name with nothing, so a recovered meeting already has them. A meeting saved by an older version without names is held as `recording` (still blocking a new recording and the job's final pass) until the bot has logged in, then its speakers are looked up in the guild and it is queued; anyone still unknown appears as `Ομιλητής <id>`.
- **Cleanup.** Audio for a chunk is deleted once its transcript is written. The transcript and summary are kept as text until an officer deletes them by hand; there is no automatic expiry.

## Resources and operations

Between meetings the feature uses no CPU or RAM; its only permanent cost is about 10 GB of disk for the two models.

| Stage | CPU | RAM (approximate) |
| --- | --- | --- |
| Idle | none | none |
| Recording | light (bot writes audio files) | small |
| Transcribing | up to 8 threads, lowest priority | 3-4 GB |
| Summarizing | up to 8 threads, lowest priority | about 8 GB |

Containers:

- A `worker` image (Python, `faster-whisper`, `ffmpeg`) runs once per meeting and exits.
- `ollama` starts for the summary stage and stops afterwards; `keep_alive` is 0 so the model leaves memory immediately.
- Models live in named volumes (about 1.6 GB for Whisper, about 8 GB for Gemma), so they download once.
- The job runs at `nice 19` with a container CPU and memory limit, so other services on the server always win.

How the job is launched (decided): a systemd path unit on the host that watches the meetings directory and starts a oneshot `docker compose run` when a meeting is queued. Nothing runs while idle, and the bot does not need access to the Docker socket. The alternative, mounting the Docker socket into the bot, is simpler but gives the bot control over every container on the server.

## Benchmark results

Turbo with 8 threads and beam 1 reaches 1.2x realtime at 4.1% word error rate, which is fast and accurate enough. All numbers come from a 25-second synthetic Greek clip (text-to-speech) on the target server, so real recordings are still untested. The benchmark lives in `scripts/benchmark_models.py` and is run with `scripts/run_benchmark.sh`.

| Test | Speed | Quality |
| --- | --- | --- |
| `large-v3`, 4 threads, beam 5 | 3.5x realtime | WER 8.2% |
| `large-v3-turbo`, 4 threads, beam 5 | 1.8x realtime | WER 9.6% |
| `large-v3-turbo`, 8 threads, beam 1, vocabulary prompt | 1.2x realtime | WER 4.1% |
| Gemma 3 12B, summary of the clip | 2.7 tokens/s writing, 7.2 tokens/s reading | Correct Greek, structure and deadlines |

"1.2x realtime" means 1.2 seconds of processing per second of audio.

Expected time for a 1-hour meeting (estimates, not measurements):

- Transcription runs during the call, leaving about 10-15 minutes after it ends.
- Summary: Greek is about 2.2 tokens per word, so roughly 17,000 tokens to read (about 40 minutes) plus 15-20 minutes of writing.
- Total after the meeting ends: about 1-1.5 hours.

## Risks, privacy and open questions

The biggest technical risk is voice receiving; the biggest non-technical one is consent.

| Risk | Mitigation |
| --- | --- |
| `discord-ext-voice-recv` fails or breaks after a Discord protocol change (it already lacks DAVE decryption) | Own decryption step, pinned versions, `/record stop` reports decrypt statistics; fallback is a small separate recorder (for example Node with `@discordjs/voice`) |
| Greek accuracy drops on real calls (noise, overlapping speakers, Whisper inventing text in silence) | Silence filtering, vocabulary prompt, test on a real recording before release |
| Summary invents or misattributes a task | Prompt forbids adding information; full transcript always attached |
| Summary too slow | Swap in a smaller model (Gemma 3 4B, Llama-Krikri-8B) via config, accepting lower accuracy |
| Recording people without notice (GDPR and Discord rules) | Announcement on start, officer-only commands, audio deleted after transcription. Transcripts stay until manually deleted, so officers own that cleanup |
| Job competes with other services | Lowest priority, CPU and memory limits, runs about twice a month |

Decided:

- Consent: the announcement in the voice channel's text chat is enough.
- Only officers start and stop recordings, and only one recording can exist at a time.
- The summaries channel is set by officers with `/channel set`.
- Transcripts and summaries are kept until deleted manually.
- The job is launched by a systemd path unit on the host; the bot gets no Docker access.

Summary model (decided from `scripts/run_summary_benchmark.sh` on the sample meeting): Gemma 3 12B. It was the only model to get all 9 facts. Gemma 3 4B was 3x faster but dropped the budget and invented a date; Llama-Krikri-8B was in between on speed but put tasks under decisions and missed some facts. The summary prompt should also say to list only tasks that were explicitly assigned, since the 12B model added one inferred task.

Open questions: none. What is still unproven is the voice-receive library (step 1 of the plan) and accuracy on a real Greek recording.

## Implementation plan

Six steps, each ending in something that can be tested. Step 1 gates everything else.

1. **Recording spike.** *Code written and unit-tested; needs a live test.* `/record start` and `/record stop` write per-speaker segments. Done when two people talk in a real call, `/record stop` reports decrypted frames, and each file plays back correctly. If it is unreliable, switch to the fallback recorder before going further.
2. **Meeting state.** Manifest, statuses, officer role check, single-recording guard, start announcement, `/channel set` support for the summaries channel, unit tests for chunk naming and manifest updates.
3. **Transcription job.** Worker image that transcribes chunks and builds the merged transcript. Tested on a real Greek meeting recording.
4. **Summary stage.** Piece-by-piece notes and the merge pass, Ollama start and stop, retry. Compare Gemma 3 12B with Krikri and Gemma 3 4B on the real transcript.
5. **Launching and limits.** Systemd path unit (or chosen alternative), CPU and memory limits, `nice`, logging, resume after a crash.
6. **Posting and polish.** Status message, summary and `.txt` attachment, audio cleanup, README and `config.example.toml` updates.
