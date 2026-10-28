"""Posting a processed meeting to the summaries channel: a status message, then the summary and the transcript."""
from __future__ import annotations

import re

import discord

from bot.recording.meeting import Meeting

LIMIT = 2000  # Discord's message length limit
HEADING = re.compile(r"\s*(#{1,6}\s|\*\*[^*]+\*\*:?\s*$)")  # "## Θέματα" or a line that is all bold
NO_MENTIONS = discord.AllowedMentions.none()  # a transcript can contain "@everyone"
FINAL = ("done", "failed")
INTERRUPTED_NOTE = "⚠️ Η ηχογράφηση διακόπηκε από επανεκκίνηση του bot, οπότε μπορεί να είναι ελλιπής."
STATUS_TEXT = {  # the status message, in Greek like the summary
    "queued": "⏳ Συνάντηση {when}: σε αναμονή για επεξεργασία.",
    "transcribing": "🎙️ Συνάντηση {when}: απομαγνητοφώνηση σε εξέλιξη...",
    "summarizing": "📝 Συνάντηση {when}: η απομαγνητοφώνηση τελείωσε, γράφεται η σύνοψη...",
    "done": "✅ Συνάντηση {when}: η επεξεργασία ολοκληρώθηκε.",
    "failed": "❌ Συνάντηση {when}: η επεξεργασία απέτυχε.",
}


def split_summary(text: str, limit: int = LIMIT) -> list[str]:
    """Split into messages of at most `limit` characters, breaking between paragraphs and before headings."""
    messages: list[str] = []
    for block in _blocks(text):
        if len(block) > limit:
            messages.extend(_cut(block, limit))
        elif messages and len(messages[-1]) + 2 + len(block) <= limit:
            messages[-1] += "\n\n" + block
        else:
            messages.append(block)
    return messages


def _blocks(text: str):
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        current: list[str] = []
        for line in paragraph.split("\n"):
            if current and HEADING.match(line):
                yield "\n".join(current)
                current = []
            current.append(line)
        yield "\n".join(current)


def _cut(block: str, limit: int) -> list[str]:
    """A block too long for one message: split it between lines, and cut lines that are longer than a message."""
    pieces: list[str] = []
    for line in block.split("\n"):
        for start in range(0, max(len(line), 1), limit):
            chunk = line[start:start + limit]
            if pieces and len(pieces[-1]) + 1 + len(chunk) <= limit:
                pieces[-1] += "\n" + chunk
            else:
                pieces.append(chunk)
    return pieces


def _when(meeting: Meeting) -> str:
    return f"{meeting.started_at:%d/%m/%Y %H:%M}"


async def post_update(channel, meeting: Meeting) -> None:
    """Bring the summaries channel up to date with the meeting.

    Safe to call again after an error (discord.HTTPException, or OSError for a missing result file): what was
    already posted is recorded in the meeting, so nothing is posted twice.
    """
    if meeting.posted or meeting.status not in STATUS_TEXT:
        return
    await _show_status(channel, meeting)
    if meeting.status == "done":
        await _post_result(channel, meeting)
    elif meeting.status == "failed":
        reason = f" Αιτία: {meeting.error[:300]}" if meeting.error else ""
        note = f"\n{INTERRUPTED_NOTE}" if meeting.interrupted else ""
        await channel.send(
            f"❌ Η επεξεργασία της συνάντησης {_when(meeting)} (`{meeting.id}`) απέτυχε.{reason} "
            f"Δείτε το log της επεξεργασίας στον server.{note}",
            allowed_mentions=NO_MENTIONS,
        )
        meeting.mark_posted()


async def _show_status(channel, meeting: Meeting) -> None:
    if meeting.status_shown == meeting.status:
        return
    text = STATUS_TEXT[meeting.status].format(when=_when(meeting))
    if meeting.status_message_id is not None:
        try:
            await channel.get_partial_message(meeting.status_message_id).edit(content=text)
            meeting.set_status_message(meeting.status_message_id, meeting.status)
            return
        except discord.NotFound:
            pass  # someone deleted it
    if meeting.status in FINAL:
        return  # the result itself follows; a status message would only repeat it
    message = await channel.send(text, allowed_mentions=NO_MENTIONS)
    meeting.set_status_message(message.id, meeting.status)


async def _post_result(channel, meeting: Meeting) -> None:
    summary = meeting.summary_path.read_text(encoding="utf-8")
    if not meeting.transcript_path.is_file():
        raise FileNotFoundError(meeting.transcript_path)  # check before posting anything
    header = f"**Σύνοψη συνάντησης {_when(meeting)}**"
    if meeting.interrupted:
        header += f"\n{INTERRUPTED_NOTE}"
    parts = split_summary(f"{header}\n\n{summary}")
    for number in range(meeting.parts_sent, len(parts)):
        await channel.send(parts[number], allowed_mentions=NO_MENTIONS)
        meeting.set_parts_sent(number + 1)
    await channel.send(
        "Η πλήρης απομαγνητοφώνηση:",
        file=discord.File(meeting.transcript_path, filename=f"transcript-{meeting.id}.txt"),
        allowed_mentions=NO_MENTIONS,
    )
    meeting.mark_posted()
