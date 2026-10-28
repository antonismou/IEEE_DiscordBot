import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import discord
import pytest

from bot.recording.meeting import Meeting
from bot.recording.posting import post_update, split_summary

NOW = datetime(2026, 10, 25, 18, 30, 5)
SUMMARY = "## Θέματα\n- Προϋπολογισμός\n\n## Αποφάσεις\n- Workshop στις 12/11\n\n## Εργασίες\n- Μαρία: αίθουσα\n"


class FakeChannel:
    """A text channel that remembers what was posted, and the status message's edits."""

    def __init__(self):
        self.sent = []
        self.edits = []
        self.deleted = set()
        self.fail_on = None  # fail the nth send (1-based), once

    async def send(self, content=None, **kwargs):
        self.sent.append(SimpleNamespace(content=content, **kwargs))
        if self.fail_on == len(self.sent):
            self.fail_on = None
            self.sent.pop()
            raise discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "boom")
        return SimpleNamespace(id=1000 + len(self.sent))

    def get_partial_message(self, message_id):
        async def edit(content=None, **kwargs):
            if message_id in self.deleted:
                raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "Unknown Message")
            self.edits.append((message_id, content))

        return SimpleNamespace(id=message_id, edit=edit)


def meeting_with(tmp_path, status, summary=SUMMARY, transcript="[00:00:01] Μαρία: Καλησπέρα\n"):
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    meeting.set_status(status)
    if summary is not None:
        meeting.summary_path.write_text(summary, encoding="utf-8")
    if transcript is not None:
        meeting.transcript_path.write_text(transcript, encoding="utf-8")
    return meeting


def run(channel, meeting):
    asyncio.run(post_update(channel, meeting))
    return Meeting.load(meeting.directory)        # as the next loop (or a restarted bot) sees it


def advance(meeting, status):
    meeting.set_status(status)
    return meeting


def test_splitting_keeps_short_text_whole():
    assert split_summary("## Θέματα\n- ένα") == ["## Θέματα\n- ένα"]


def test_splitting_breaks_at_headings_and_paragraphs_and_never_exceeds_the_limit():
    sections = [f"## Ενότητα {n}\n" + "\n".join(f"- σημείο {n}.{i} " + "λ" * 40 for i in range(12)) for n in range(6)]
    parts = split_summary("\n\n".join(sections), limit=1000)

    assert len(parts) > 1 and all(len(part) <= 1000 for part in parts)
    assert all(part.startswith("## Ενότητα") for part in parts)      # each message starts at a heading
    assert "\n\n".join(parts) == "\n\n".join(sections)               # nothing lost


def test_splitting_a_heading_glued_to_the_previous_paragraph_still_breaks_before_it():
    text = "## Θέματα\n" + "α" * 60 + "\n## Αποφάσεις\n" + "β" * 60
    assert split_summary(text, limit=100) == ["## Θέματα\n" + "α" * 60, "## Αποφάσεις\n" + "β" * 60]


def test_splitting_a_huge_paragraph_falls_back_to_lines_and_then_hard_cuts():
    lines = ["γ" * 300 for _ in range(10)] + ["δ" * 2500]
    parts = split_summary("\n".join(lines), limit=1000)
    assert all(len(part) <= 1000 for part in parts)
    assert "".join(parts).replace("\n", "") == "".join(lines)


def test_a_queued_meeting_gets_a_status_message_that_is_edited_as_processing_advances(tmp_path):
    channel = FakeChannel()
    meeting = run(channel, meeting_with(tmp_path, "queued"))
    assert len(channel.sent) == 1 and "αναμονή" in channel.sent[0].content
    assert meeting.status_message_id == 1001 and meeting.status_shown == "queued"

    run(channel, meeting)                                              # nothing changed: nothing to do
    meeting = run(channel, advance(meeting, "transcribing"))
    meeting = run(channel, advance(meeting, "summarizing"))

    assert len(channel.sent) == 1
    assert [message_id for message_id, _ in channel.edits] == [1001, 1001]
    assert "απομαγνητοφώνηση" in channel.edits[0][1].lower() and "σύνοψη" in channel.edits[1][1].lower()
    assert meeting.status_shown == "summarizing" and not meeting.posted


def test_a_deleted_status_message_is_posted_again(tmp_path):
    channel = FakeChannel()
    meeting = run(channel, meeting_with(tmp_path, "queued"))
    channel.deleted.add(meeting.status_message_id)

    meeting = run(channel, advance(meeting, "transcribing"))

    assert len(channel.sent) == 2 and meeting.status_message_id == 1002 and meeting.status_shown == "transcribing"


def test_a_finished_meeting_posts_the_summary_and_the_transcript_once(tmp_path):
    channel = FakeChannel()
    meeting = run(channel, meeting_with(tmp_path, "queued"))
    meeting = run(channel, advance(meeting, "done"))

    assert "ολοκληρώθηκε" in channel.edits[-1][1].lower()
    summary, transcript = channel.sent[1:]
    assert "Αποφάσεις" in summary.content and "25/10/2026" in summary.content
    assert transcript.file.filename == f"transcript-{meeting.id}.txt"
    assert all(m.allowed_mentions.to_dict() == {"parse": []} for m in channel.sent)   # no pings
    assert meeting.posted

    run(channel, meeting)
    assert len(channel.sent) == 3                                       # never twice


def test_a_long_summary_is_posted_in_several_messages(tmp_path):
    channel = FakeChannel()
    long_summary = "\n\n".join(f"## Θέμα {n}\n" + "- " + "ε" * 900 for n in range(5))
    run(channel, meeting_with(tmp_path, "done", summary=long_summary))

    *summary, transcript = channel.sent
    assert len(summary) > 1 and all(len(m.content) <= 2000 for m in summary)
    assert transcript.file is not None


def test_a_failed_post_is_retried_without_repeating_what_was_already_sent(tmp_path):
    channel = FakeChannel()
    long_summary = "\n\n".join(f"## Θέμα {n}\n" + "- " + "ε" * 900 for n in range(5))
    meeting = meeting_with(tmp_path, "done", summary=long_summary)
    channel.fail_on = 2                                                 # the second summary message fails

    with pytest.raises(discord.HTTPException):
        asyncio.run(post_update(channel, meeting))
    meeting = Meeting.load(meeting.directory)
    assert not meeting.posted and meeting.parts_sent == 1

    meeting = run(channel, meeting)
    contents = [m.content for m in channel.sent]
    assert meeting.posted and len(contents) == len(set(contents))      # each part exactly once


def test_a_missing_result_file_posts_nothing_and_stays_unposted(tmp_path):
    channel = FakeChannel()
    meeting = meeting_with(tmp_path, "done", transcript=None)

    with pytest.raises(OSError):
        asyncio.run(post_update(channel, meeting))
    assert channel.sent == [] and not Meeting.load(meeting.directory).posted


def test_a_failed_meeting_posts_one_greek_notice(tmp_path):
    channel = FakeChannel()
    meeting = meeting_with(tmp_path, "queued", summary=None, transcript=None)
    meeting = run(channel, meeting)
    data = json.loads((meeting.directory / "manifest.json").read_text(encoding="utf-8"))
    data.update(status="failed", error="Ollama returned 500")
    (meeting.directory / "manifest.json").write_text(json.dumps(data), encoding="utf-8")

    meeting = run(channel, Meeting.load(meeting.directory))
    run(channel, meeting)

    [_, notice] = channel.sent
    assert "απέτυχε" in notice.content and "Ollama returned 500" in notice.content and meeting.posted


def test_a_failed_meeting_that_is_reprocessed_is_posted_again(tmp_path):
    """deploy/README.md: "To reprocess a meeting, set its status in manifest.json back to queued". The usual case
    is a meeting that failed (a model not pulled yet) and is reprocessed after the fix; here the job finishes it
    before the bot looks again."""
    meeting = meeting_with(tmp_path, "failed")
    channel = FakeChannel()
    assert run(channel, meeting).posted                                 # the failure notice

    Meeting.load(meeting.directory).set_status("queued")                # the reprocess step; the job then finishes
    Meeting.load(meeting.directory).set_status("done")
    meeting = run(channel, Meeting.load(meeting.directory))

    notice, summary, transcript = channel.sent
    assert "απέτυχε" in notice.content and "Αποφάσεις" in summary.content and transcript.file is not None
    assert meeting.posted


def test_a_reprocess_the_bot_sees_running_gets_a_new_status_message_and_a_new_result(tmp_path):
    channel = FakeChannel()
    meeting = run(channel, meeting_with(tmp_path, "done"))
    assert len(channel.sent) == 2

    meeting = run(channel, advance(Meeting.load(meeting.directory), "transcribing"))
    assert len(channel.sent) == 3 and "απομαγνητοφώνηση" in channel.sent[2].content and not meeting.posted

    meeting = run(channel, advance(meeting, "done"))                   # the same summary: still the new run's result
    assert len(channel.sent) == 5 and "Αποφάσεις" in channel.sent[3].content and meeting.posted
    assert run(channel, Meeting.load(meeting.directory)) and len(channel.sent) == 5


def test_a_new_summary_written_while_the_bot_was_not_looking_is_posted_again(tmp_path):
    channel = FakeChannel()
    meeting = run(channel, meeting_with(tmp_path, "done"))
    meeting.summary_path.write_text(SUMMARY.replace("12/11", "19/11"), encoding="utf-8")   # a full rerun

    run(channel, Meeting.load(meeting.directory))

    assert len(channel.sent) == 4 and "19/11" in channel.sent[2].content


def test_a_posted_meeting_is_not_posted_again_after_a_bot_restart(tmp_path):
    channel = FakeChannel()
    for status in ("done", "failed"):
        meeting = meeting_with(tmp_path / status, status)
        run(channel, meeting)
        before = len(channel.sent)
        run(channel, Meeting.load(meeting.directory))                   # a restarted bot reads it from disk
        run(channel, Meeting.load(meeting.directory))
        assert len(channel.sent) == before


def test_a_meeting_posted_by_an_older_version_is_not_posted_again(tmp_path):
    channel = FakeChannel()
    meeting = meeting_with(tmp_path, "done")
    (meeting.directory / "posting.json").write_text('{"posted": true}', encoding="utf-8")   # no result token

    run(channel, Meeting.load(meeting.directory))
    assert channel.sent == []


@pytest.mark.parametrize("status", ["recording", "interrupted"])
def test_meetings_that_are_not_being_processed_are_left_alone(tmp_path, status):
    channel = FakeChannel()
    run(channel, meeting_with(tmp_path, status))
    assert channel.sent == [] and channel.edits == []


def test_a_meeting_found_already_done_gets_no_separate_status_message(tmp_path):
    channel = FakeChannel()
    run(channel, meeting_with(tmp_path, "done"))
    assert len(channel.sent) == 2 and channel.edits == []                # summary + transcript only


def test_a_recording_cut_short_by_a_restart_says_so_above_its_summary(tmp_path):
    channel = FakeChannel()
    meeting = meeting_with(tmp_path, "done")
    meeting.mark_interrupted()

    run(channel, meeting)

    summary, _ = channel.sent
    assert summary.content.startswith("**Σύνοψη συνάντησης")
    assert "επανεκκίνηση" in summary.content and "ελλιπής" in summary.content
    assert all(m.allowed_mentions.to_dict() == {"parse": []} for m in channel.sent)


def test_a_normal_recording_has_no_interruption_note(tmp_path):
    channel = FakeChannel()
    run(channel, meeting_with(tmp_path, "done"))
    assert "επανεκκίνηση" not in channel.sent[0].content


def test_the_interruption_note_still_fits_a_long_summary_into_discord_messages(tmp_path):
    channel = FakeChannel()
    long_summary = "ε" * 1990                                         # one block that alone nearly fills a message
    meeting = meeting_with(tmp_path, "done", summary=long_summary)
    meeting.mark_interrupted()

    run(channel, meeting)

    *summary, _ = channel.sent
    assert all(len(m.content) <= 2000 for m in summary) and "επανεκκίνηση" in summary[0].content
