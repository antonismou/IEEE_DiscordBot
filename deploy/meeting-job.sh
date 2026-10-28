#!/bin/sh
# Runs one processing job; ExecStart of ieee-meeting-worker.service, from the folder that holds docker-compose.yml.
#   deploy/meeting-job.sh /path/to/data/meetings
# A meeting that has ended (queued, or transcribing/summarizing left by a crash) needs the full job, which starts
# Ollama through the worker's depends_on. Otherwise only a call is still recording: the job runs with --live and
# --no-deps, so it transcribes the finished segments and Ollama (12 GB) is not started during the call. Summarizing
# happens only after the meeting has ended.
# A meeting stopped while a live run is busy is picked up by the next tick, which then sees it queued.
dir="${1:?usage: meeting-job.sh MEETINGS_DIR}"
here="$(dirname "$0")"
run="docker compose --profile job run --rm -T --name ieee-meeting-worker"
if PENDING_STATUSES="queued|transcribing|summarizing" sh "$here/meeting-pending.sh" "$dir"; then
    exec $run worker
fi
exec $run --no-deps worker python -m worker.run --live
