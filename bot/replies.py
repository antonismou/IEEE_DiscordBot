from __future__ import annotations

import discord
from discord.utils import MISSING


async def defer(interaction: discord.Interaction, *, replace: bool) -> None:
    """Acknowledge a button/menu/form press. `replace` means the press came from one of our private messages,
    which is then edited in place instead of a new one being added below it."""
    if replace:
        await interaction.response.defer()
    else:
        await interaction.response.defer(ephemeral=True, thinking=True)


async def reply(interaction: discord.Interaction, text: str, *, replace: bool, view=MISSING) -> None:
    """Show `text` privately. After `defer(replace=True)` this rewrites the message the member pressed a button on.
    `view`: omit to keep the buttons the message has, None to remove them, or pass a new view."""
    if replace:
        await interaction.edit_original_response(content=text, view=view)
    else:
        await interaction.followup.send(text, ephemeral=True, view=MISSING if view is None else view)
