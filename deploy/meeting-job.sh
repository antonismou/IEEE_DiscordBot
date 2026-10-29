#!/bin/sh
# Runs one processing job; ExecStart of ieee-meeting-worker.service, from the folder that holds docker-compose.yml.
#   deploy/meeting-job.sh /path/to/data/meetings
# Also the way to run the job by hand (stop the timer first, see deploy/README.md).
# A meeting that has ended (queued, or transcribing/summarizing left by a crash) needs the full job, which starts
# Ollama through the worker's depends_on and stops it again afterwards. Otherwise only a call is still recording:
# the job runs with --live and --no-deps, so it transcribes the finished segments and Ollama (up to 12 GB) is not
# started during the call. Summarizing happens only after the meeting has ended.
# A meeting stopped while a live run is busy is picked up by the next tick, which then sees it queued.
#
# Ollama is stopped here, not by the unit, so that a run which finds another worker busy (exit 75) leaves that
# worker's Ollama alone, and an idle tick (skipped by ExecCondition) runs no docker command at all.
dir="${1:?usage: meeting-job.sh MEETINGS_DIR}"
here="$(dirname "$0")"
name=ieee-meeting-worker
busy=75  # the worker's exit code when another worker holds the lock (worker/run.py)
compose="docker compose --profile job"

# On a stop or a timeout systemd sends SIGTERM to this script only (KillMode=mixed): stop the job container, which
# also ends the `compose run` client, and stop Ollama if this run uses it. `docker stop` sends the worker SIGTERM
# first, so it leaves the meeting for the next run without counting the run as a crash (worker/run.py); 60 s, so all
# of it fits in the unit's TimeoutStopSec (2 min). Then the container is removed, if `--rm` has not already.
cleanup() {
    docker stop --time 60 "$name" >/dev/null 2>&1
    docker rm -f "$name" >/dev/null 2>&1
    [ -n "$full" ] && $compose stop ollama
    exit 143
}
trap cleanup TERM INT HUP

full=
if PENDING_STATUSES="queued|transcribing|summarizing" sh "$here/meeting-pending.sh" "$dir"; then
    # Another run of this script (a manual one while the service is active) owns the container and its Ollama.
    if docker container inspect "$name" >/dev/null 2>&1; then
        echo "container $name exists: another run is in progress, not starting" >&2
        exit $busy
    fi
    # Ollama already up before this run belongs to someone else if the worker then finds the lock busy.
    ollama_was_up="$($compose ps -q --status running ollama 2>/dev/null)"
    full=1
    $compose run --rm -T --name "$name" worker &
else
    $compose run --rm -T --name "$name" --no-deps worker python -m worker.run --live &
fi
wait $!
code=$?
trap - TERM INT HUP
# Stop Ollama after every full run (done, failed or killed), unless the worker found another one busy and that
# other run was already using Ollama. Stopped, not removed, so `docker compose logs ollama` still works.
if [ -n "$full" ] && { [ "$code" -ne "$busy" ] || [ -z "$ollama_was_up" ]; }; then
    $compose stop ollama
fi
exit $code
