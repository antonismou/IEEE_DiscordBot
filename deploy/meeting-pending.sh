#!/bin/sh
# Exit 0 if any meeting under the given folder still needs the processing job, 1 otherwise.
# Used as ExecCondition by ieee-meeting-worker.service, so an idle timer tick costs one grep and starts no container.
#   deploy/meeting-pending.sh /path/to/data/meetings
# The statuses can be overridden with PENDING_STATUSES (an extended regex alternation).
# "recording" is included because the worker transcribes finished segments while the call is still going.
# meeting-job.sh reuses this with PENDING_STATUSES to decide whether the run needs Ollama.
dir="${1:?usage: meeting-pending.sh MEETINGS_DIR}"
statuses="${PENDING_STATUSES:-recording|queued|transcribing|summarizing}"
# manifest.json is written by json.dumps(indent=2), so the status is on its own line as "status": "<value>".
exec grep -lqsE "^  \"status\": \"($statuses)\"" "$dir"/*/manifest.json
