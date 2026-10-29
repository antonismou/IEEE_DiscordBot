"""Edge cases for posting a meeting's result (bot/recording/posting.py), written from the design spec."""
import asyncio
import os
import shutil
from datetime import datetime
from types import SimpleNamespace

import discord
import pytest

from bot.recording.meeting import Meeting
from bot.recording.posting import LIMIT, needs_update, post_update, split_summary

NOW = datetime(2026, 10, 25, 18, 30, 5)
SUMMARY = "## Θέματα\n- Προϋπολογισμός\n\n## Αποφάσεις\n- Workshop\n\n## Εργασίες\n- Μαρία: αίθουσα\n"


def http_error(status=500):
    return discord.HTTPException(SimpleNamespace(status=status, reason="boom"), "boom")


class Channel:
    def __init__(self):
        self.sent = []          # (content, filename or None)
        self.edits = []
        self.deleted = set()
        self.fail_on = set()    # 1-based numbers of send calls that fail (counting failed ones)
        self.calls = 0

    async def send(self, content=None, file=None, **kwargs):
        self.calls += 1
        if self.calls in self.fail_on:
            raise http_error()
        if content is not None and not content.strip() and file is None:
            raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"),
                                        "Cannot send an empty message")
        if content is not None and len(content) > LIMIT:
            raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), "too long")
        self.sent.append((content, file.filename if file else None))
        return SimpleNamespace(id=1000 + len(self.sent))

    def get_partial_message(self, message_id):
        async def edit(content=None, **kwargs):
            if message_id in self.deleted:
                raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "Unknown Message")
            self.edits.append((message_id, content))

        return SimpleNamespace(id=message_id, edit=edit)

    @property
    def files(self):
        return [name for _, name in self.sent if name]

    @property
    def texts(self):
        return [content for content, name in self.sent if not name]


def meeting_with(root, status, summary=SUMMARY, transcript="[00:00:01] Μαρία: Καλησπέρα\n", error=None):
    meeting = Meeting.create(root, 11, 22, NOW)
    if error:
        meeting.set_error(error)
    meeting.set_status(status)
    if summary is not None:
        meeting.summary_path.write_text(summary, encoding="utf-8")
    if transcript is not None:
        meeting.transcript_path.write_text(transcript, encoding="utf-8")
    return meeting


def post(channel, meeting):
    asyncio.run(post_update(channel, Meeting.load(meeting.directory)))
    return Meeting.load(meeting.directory)


def set_status(meeting, status):
    Meeting.load(meeting.directory).set_status(status)


# --- splitting ----------------------------------------------------------------------------------------------------

def joined_back(parts):
    return "".join(part.replace("\n", "") for part in parts)


def test_a_block_of_exactly_the_limit_is_one_message():
    text = "α" * LIMIT
    assert split_summary(text) == [text]


def test_one_character_over_the_limit_is_two_messages_and_nothing_is_lost():
    text = "α" * (LIMIT + 1)
    parts = split_summary(text)
    assert len(parts) == 2 and all(len(p) <= LIMIT for p in parts) and "".join(parts) == text


def test_two_paragraphs_that_exactly_fit_together_share_a_message_and_one_more_splits_them():
    first = "α" * 1000
    fits = "β" * (LIMIT - 1000 - 2)
    assert split_summary(f"{first}\n\n{fits}") == [f"{first}\n\n{fits}"]
    over = fits + "β"
    assert split_summary(f"{first}\n\n{over}") == [first, over]


def test_lines_in_one_long_paragraph_are_kept_whole_up_to_the_limit():
    lines = ["- " + "λ" * 97 for _ in range(50)]          # 99 chars + newline each
    parts = split_summary("\n".join(lines))
    assert all(len(p) <= LIMIT for p in parts) and len(parts) > 1
    assert [line for part in parts for line in part.split("\n")] == lines


@pytest.mark.parametrize("char", ["😀", "👩‍💻", "ά", "€", "́"])
def test_multi_byte_characters_never_make_a_message_too_long_or_get_lost(char):
    text = "## Θέματα\n" + char * 3000 + "\n\n## Αποφάσεις\n- " + char * 10
    parts = split_summary(text)
    assert all(len(p) <= LIMIT for p in parts)
    assert joined_back(parts) == text.replace("\n", "")


def test_a_very_long_summary_is_split_without_loss_and_without_empty_messages():
    sections = []
    for n in range(40):
        sections.append(f"## Ενότητα {n}\n" + "\n".join(f"- σημείο {i} " + "κ" * (n * 7 % 300) for i in range(30)))
    text = "\n\n".join(sections)
    parts = split_summary(text)
    assert all(0 < len(p) <= LIMIT and p.strip() for p in parts)
    assert joined_back(parts) == text.replace("\n", "")


def test_a_line_just_over_the_limit_followed_by_spaces_does_not_produce_a_blank_message():
    """Discord refuses a message that is only whitespace ("Cannot send an empty message")."""
    text = "## Θέματα\n" + "α" * LIMIT + "   \n\n## Αποφάσεις\n" + "β" * (LIMIT + 10)
    parts = split_summary(text)
    assert all(p.strip() for p in parts), [len(p) for p in parts]


def test_such_a_summary_is_still_posted_in_the_end(tmp_path):
    summary = "## Θέματα\n" + "α" * LIMIT + "   \n\n## Αποφάσεις\n" + "β" * (LIMIT + 10)
    meeting = meeting_with(tmp_path, "done", summary=summary)
    channel = Channel()
    for _ in range(3):
        try:
            post(channel, meeting)
        except discord.HTTPException:
            pass                                                         # the poster retries on its next turn
    assert Meeting.load(meeting.directory).posted


def test_an_empty_summary_still_has_its_header_message():
    assert split_summary("**Σύνοψη συνάντησης 25/10/2026 18:30**\n\n") == ["**Σύνοψη συνάντησης 25/10/2026 18:30**"]


# --- posting a result ---------------------------------------------------------------------------------------------

def test_a_long_summary_is_posted_in_parts_then_the_transcript(tmp_path):
    summary = "\n\n".join(f"## Ενότητα {n}\n" + "- " + "μ" * 1500 for n in range(5))
    meeting = meeting_with(tmp_path, "done", summary=summary)
    channel = Channel()

    after = post(channel, meeting)

    assert after.posted and channel.files == [f"transcript-{meeting.id}.txt"]
    assert channel.sent[-1][1] is not None                               # the transcript comes last
    assert len(channel.texts) >= 5 and all(len(t) <= LIMIT for t in channel.texts)


def test_an_empty_transcript_and_empty_summary_are_still_posted(tmp_path):
    meeting = meeting_with(tmp_path, "done", summary="", transcript="")
    channel = Channel()

    after = post(channel, meeting)

    assert after.posted and len(channel.texts) == 1 and channel.files == [f"transcript-{meeting.id}.txt"]


def test_a_crash_between_parts_resumes_without_repeating_parts(tmp_path):
    summary = "\n\n".join(f"## Ενότητα {n}\n" + "- " + "ν" * 1500 for n in range(4))
    meeting = meeting_with(tmp_path, "done", summary=summary)
    channel = Channel()
    channel.fail_on = {3}                                                # the 3rd part fails

    with pytest.raises(discord.HTTPException):
        post(channel, meeting)
    assert not Meeting.load(meeting.directory).posted
    after = post(channel, meeting)                                       # as a restarted bot would

    assert after.posted
    assert len(channel.texts) == len(set(channel.texts)) == len(split_summary(
        f"**Σύνοψη συνάντησης 25/10/2026 18:30**\n\n{summary}"))
    assert channel.files == [f"transcript-{meeting.id}.txt"]


def test_a_failed_transcript_upload_retries_only_the_transcript(tmp_path):
    meeting = meeting_with(tmp_path, "done")
    channel = Channel()
    parts = len(split_summary(f"**Σύνοψη συνάντησης 25/10/2026 18:30**\n\n{SUMMARY}"))
    channel.fail_on = {parts + 1}

    with pytest.raises(discord.HTTPException):
        post(channel, meeting)
    post(channel, meeting)

    assert len(channel.texts) == parts and len(channel.files) == 1


def test_a_missing_transcript_posts_nothing_of_the_result(tmp_path):
    meeting = meeting_with(tmp_path, "done", transcript=None)
    channel = Channel()
    with pytest.raises(OSError):
        post(channel, meeting)
    assert channel.sent == [] and not Meeting.load(meeting.directory).posted


def test_an_unreadable_summary_is_not_posted_and_is_posted_once_readable(tmp_path):
    meeting = meeting_with(tmp_path, "done")
    meeting.summary_path.write_bytes(b"\xff\xfe broken \x80")
    channel = Channel()

    with pytest.raises((OSError, ValueError)):
        post(channel, meeting)
    assert channel.sent == [] and not Meeting.load(meeting.directory).posted

    meeting.summary_path.write_text(SUMMARY, encoding="utf-8")
    after = post(channel, meeting)
    assert after.posted and len(channel.files) == 1


def test_a_posted_meeting_whose_summary_becomes_unreadable_keeps_its_record(tmp_path):
    meeting = meeting_with(tmp_path, "done")
    channel = Channel()
    post(channel, meeting)
    meeting.summary_path.write_bytes(b"\xff\xfe")
    loaded = Meeting.load(meeting.directory)

    assert not needs_update(loaded)
    after = post(channel, meeting)
    assert after.posted and after.posted_for == loaded.posted_for and len(channel.files) == 1


# --- failure notices ----------------------------------------------------------------------------------------------

def test_a_failed_meeting_is_announced_once_even_after_many_turns(tmp_path):
    meeting = meeting_with(tmp_path, "failed", summary=None, transcript=None, error="RuntimeError: boom")
    channel = Channel()
    for _ in range(4):
        post(channel, meeting)
    assert len(channel.sent) == 1 and "boom" in channel.sent[0][0]


def test_a_failed_meeting_with_a_status_message_edits_it_and_posts_one_notice(tmp_path):
    meeting = meeting_with(tmp_path, "queued", summary=None, transcript=None)
    channel = Channel()
    post(channel, meeting)
    set_status(meeting, "transcribing")
    post(channel, meeting)
    Meeting.load(meeting.directory).set_error("WorkerError: gave up")
    set_status(meeting, "failed")
    post(channel, meeting)
    post(channel, meeting)

    assert len(channel.texts) == 2                                       # the status message and one notice
    assert len({message_id for message_id, _ in channel.edits}) == 1 and len(channel.edits) == 2
    assert "gave up" in channel.texts[1]


def test_a_notice_whose_send_fails_is_retried_and_then_sent_once(tmp_path):
    meeting = meeting_with(tmp_path, "failed", summary=None, transcript=None, error="x")
    channel = Channel()
    channel.fail_on = {1}
    with pytest.raises(discord.HTTPException):
        post(channel, meeting)
    post(channel, meeting)
    post(channel, meeting)
    assert len(channel.sent) == 1


def test_a_failed_meeting_with_no_reason_is_announced(tmp_path):
    meeting = meeting_with(tmp_path, "failed", summary=None, transcript=None)
    channel = Channel()
    after = post(channel, meeting)
    assert after.posted and len(channel.sent) == 1


# --- reprocessing and the posted_for token ------------------------------------------------------------------------

def test_a_reprocess_seen_running_is_posted_again_with_a_new_status_message(tmp_path):
    meeting = meeting_with(tmp_path, "queued")
    channel = Channel()
    post(channel, meeting)
    first_status = channel.sent[0]
    set_status(meeting, "done")
    post(channel, meeting)
    edits_before = list(channel.edits)

    set_status(meeting, "queued")                                        # an officer requeues it
    post(channel, meeting)
    assert channel.edits == edits_before                                 # the old status message is left alone
    assert len([t for t in channel.texts if t.startswith("⏳")]) == 2     # a new one
    set_status(meeting, "done")                                          # same summary as before
    after = post(channel, meeting)

    assert after.posted and len(channel.files) == 2
    assert first_status in channel.sent


def test_a_reprocess_not_seen_running_is_posted_again_only_if_the_result_changed(tmp_path):
    meeting = meeting_with(tmp_path, "done")
    channel = Channel()
    post(channel, meeting)
    set_status(meeting, "queued")
    set_status(meeting, "done")                                          # finished before the bot looked
    post(channel, meeting)
    assert len(channel.files) == 1                                       # same result: not repeated

    meeting.summary_path.write_text(SUMMARY + "\n## Ανοιχτά θέματα\n- νέο\n", encoding="utf-8")
    post(channel, meeting)
    post(channel, meeting)
    assert len(channel.files) == 2
    assert any("νέο" in t for t in channel.texts)


def test_a_failed_meeting_reprocessed_to_done_unseen_posts_the_summary(tmp_path):
    meeting = meeting_with(tmp_path, "failed", summary=None, error="SummaryError: model failed")
    channel = Channel()
    post(channel, meeting)
    meeting.summary_path.write_text(SUMMARY, encoding="utf-8")
    set_status(meeting, "done")
    after = post(channel, meeting)
    assert after.posted and len(channel.files) == 1


def test_a_meeting_failing_again_for_a_new_reason_unseen_is_announced_again(tmp_path):
    meeting = meeting_with(tmp_path, "failed", summary=None, transcript=None, error="first reason")
    channel = Channel()
    post(channel, meeting)
    Meeting.load(meeting.directory).set_error("second reason")
    post(channel, meeting)
    post(channel, meeting)
    assert len(channel.sent) == 2 and "second reason" in channel.sent[1][0]


def test_a_done_and_a_failed_result_with_the_same_text_have_different_tokens(tmp_path):
    """If a meeting's failure reason happened to equal its later summary, the change must still be posted."""
    meeting = meeting_with(tmp_path, "failed", summary=None, transcript="x", error=SUMMARY)
    channel = Channel()
    post(channel, meeting)
    meeting.summary_path.write_text(SUMMARY, encoding="utf-8")
    set_status(meeting, "done")
    post(channel, meeting)
    assert len(channel.files) == 1


def test_moving_or_restoring_the_meetings_folder_never_reposts(tmp_path):
    meeting = meeting_with(tmp_path / "a", "done")
    channel = Channel()
    post(channel, meeting)
    copy = tmp_path / "b" / meeting.id
    shutil.copytree(meeting.directory, copy, copy_function=shutil.copy)  # new file times
    for path in copy.iterdir():
        os.utime(path, (1, 1))
    moved = Meeting.load(copy)
    assert not needs_update(moved)
    asyncio.run(post_update(channel, moved))
    assert len(channel.files) == 1


def test_a_meeting_posted_by_an_older_version_without_a_token_is_never_reposted(tmp_path):
    meeting = meeting_with(tmp_path, "done")
    Meeting.load(meeting.directory)._set_posting(posted=True)            # as an older version recorded it
    meeting.summary_path.write_text("## Θέματα\n- κάτι άλλο\n", encoding="utf-8")
    channel = Channel()
    post(channel, meeting)
    assert channel.sent == []


def test_a_deleted_status_message_gets_a_new_one_while_processing(tmp_path):
    meeting = meeting_with(tmp_path, "queued")
    channel = Channel()
    after = post(channel, meeting)
    channel.deleted.add(after.status_message_id)
    set_status(meeting, "summarizing")
    after = post(channel, meeting)
    assert len(channel.texts) == 2 and after.status_message_id != 1001
    set_status(meeting, "done")
    post(channel, meeting)
    assert len(channel.files) == 1


def test_a_recording_meeting_is_not_posted(tmp_path):
    meeting = meeting_with(tmp_path, "recording", summary=None, transcript=None)
    channel = Channel()
    post(channel, meeting)
    assert channel.sent == []


def test_an_interrupted_meeting_says_so_with_its_summary_and_after_a_repost(tmp_path):
    meeting = meeting_with(tmp_path, "done")
    Meeting.load(meeting.directory).mark_interrupted()
    channel = Channel()
    post(channel, meeting)
    set_status(meeting, "queued")
    post(channel, meeting)
    set_status(meeting, "done")
    post(channel, meeting)
    headers = [t for t in channel.texts if t.startswith("**Σύνοψη")]
    assert len(headers) == 2 and all("διακόπηκε" in h for h in headers)
