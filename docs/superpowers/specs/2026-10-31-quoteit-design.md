# /quoteit and /randomquote: design

## Intent
Members want to turn a funny or ridiculous message into a shareable, unhinged meme: an animated GIF with the quote on it, a random GIF or meme behind it, and chaotic effects. It is for fun and in-jokes, so every result should look different. Success is people using it often and the result making the channel laugh.

## Goals
- `/quoteit` turns a message into a random, chaotic animated meme card and posts it in the channel.
- `/randomquote` posts a previously quoted message again with a fresh random look.
- Any member can use both. No verification or role check.
- Never fails visibly: if Giphy is unavailable the bot falls back to a local meme folder.

## Non-goals
- Not a polished meme generator with fixed templates and Impact-font layout.
- No quote voting, leaderboard, or hall-of-fame channel in this version.
- No editing or deleting of saved quotes in this version (see open calls).

## Anti-goals
- A command that errors, hangs, or posts nothing when Giphy is down or the message is odd (empty, attachment-only, very long).
- A result that looks the same every time.
- Anything that blocks the bot's event loop while the GIF is rendered.

## How people use it
- **Right-click:** message context menu, Apps, "Quote it". This is the main way.
- **Slash command:** `/quoteit message:<message link or ID>` (a slash command cannot be sent as a reply, so it cannot pick up a replied-to message; corrected during planning).
- Optional `gif:<search term>` forces the Giphy search (for example `gif:facepalm`); otherwise the term is random.
- `/randomquote` takes no required options.

## Behaviour
- The bot renders an animated GIF: background from Giphy (or a local meme if Giphy is unavailable), the quote text on top, the author's name and year as a credit, and a random mix of effects drawn from: shake, glitch, deep-fry, typewriter reveal, random stickers.
- The result is posted in the same channel as a reply to the quoted message.
- Every `/quoteit` saves the quote (text, author id and name, message link, who quoted it, time). The rendered GIF is not stored.
- `/randomquote` picks a saved quote at random and renders it again with new random choices.

## Decisions made with the user
- Animated GIF output with random GIFs and effects ("unhinged chaos"), not static cards.
- Giphy as the GIF source, with a local folder of memes as the fallback.
- Everyone may quote anyone; no verification needed.

## Constraints
- Python and `discord.py`, as the rest of the bot. Features are cogs in `bot/cogs/`, data goes through repositories on the shared SQLite database (`bot/db.py`).
- Runs in the existing Docker image (slim Python, non-root). New dependency: Pillow.
- Giphy key is `GIPHY_API_KEY` in `.env`, optional. Beta keys have a low hourly limit, so one search per command at most.
- Discord upload limit applies; the GIF must be sized and its frames/colors reduced to fit.

## Builder's calls (not yet confirmed by the user)
- Giphy results filtered to rating PG-13 or lower.
- Bundled font (one that covers Greek and Latin) and a starter set of fallback memes in the repo.
- GIF rendering runs off the event loop.
- Quoting a bot message, an empty message, or an attachment-only message gets a short friendly refusal. Very long text is trimmed with an ellipsis.
- Quoting your own message is allowed.
- A small per-user cooldown to protect the Giphy limit and the CPU.
- Saved quotes live in a new `quotes` table with a `QuoteRepo`.
- `/randomquote` with no saved quotes says so.

## Open for the builder
- Exact effect implementations and how they are combined.
- GIF dimensions, frame count, and size-reduction strategy.
- Test approach (the repo has a `tests/` suite with fakes).
