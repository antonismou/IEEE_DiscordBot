from __future__ import annotations

import io

import discord
from discord import app_commands
from discord.ext import commands

from bot.checks import officer_only
from bot.db import utcnow
from bot.members import members_to_csv
from bot.roles import set_verified_role
from bot.validation import clean_text

NO_MENTIONS = discord.AllowedMentions.none()


class OfficerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def app(self):
        return self.bot.app

    member_group = app_commands.Group(name="member", description="Stored member data", guild_only=True)

    @app_commands.command(name="verify-manual", description="Verify a member who has no @tuc.gr email")
    @app_commands.guild_only()
    @officer_only()
    async def verify_manual(self, interaction: discord.Interaction, user: discord.Member, name: str, note: str) -> None:
        if user.bot:
            await interaction.response.send_message("Bots cannot be verified.", ephemeral=True)
            return
        try:
            full_name = clean_text(name, min_len=2, max_len=100, label="Name")
            reason = clean_text(note, min_len=3, max_len=200, label="Note")
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.app.members.add_manual_member(
            discord_id=user.id, full_name=full_name, verified_by=interaction.user.id,
            note=reason, verified_at=utcnow(),
        )
        ok = await set_verified_role(
            interaction.guild, user, self.app.settings.verified_role_id,
            add=True, reason=f"Manual verification by {interaction.user}",
        )
        suffix = "" if ok else " (but I couldn't assign the role: check my permissions)"
        await interaction.response.send_message(
            f"{user.mention} verified manually as **{full_name}**{suffix}.",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @app_commands.command(name="unverify", description="Remove the Verified role (stored data is kept)")
    @app_commands.guild_only()
    @officer_only()
    async def unverify(self, interaction: discord.Interaction, user: discord.Member) -> None:
        ok = await set_verified_role(
            interaction.guild, user, self.app.settings.verified_role_id,
            add=False, reason=f"Unverified by {interaction.user}",
        )
        text = "Role removed." if ok else "I couldn't remove the role: check my permissions."
        await interaction.response.send_message(text, ephemeral=True)

    @member_group.command(name="lookup", description="Show stored data for a user")
    @officer_only()
    async def lookup(self, interaction: discord.Interaction, user: discord.User) -> None:
        member = self.app.members.get(user.id)
        if member is None:
            await interaction.response.send_message("No stored data for that user.", ephemeral=True)
            return
        lines = [
            f"**Name:** {discord.utils.escape_markdown(member.full_name)}",
            f"**Email:** {member.email or '(none, verified manually)'}",
            f"**Method:** {member.method}",
            f"**Verified at:** {member.verified_at}",
        ]
        if member.method == "manual":
            lines.append(f"**Verified by:** <@{member.verified_by}>")
            lines.append(f"**Note:** {discord.utils.escape_markdown(member.note or '')}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS)

    @member_group.command(name="export", description="Download all stored members as CSV")
    @officer_only()
    async def export(self, interaction: discord.Interaction) -> None:
        data = members_to_csv(self.app.members.all()).encode("utf-8")
        await interaction.response.send_message(
            file=discord.File(io.BytesIO(data), filename="members.csv"), ephemeral=True
        )

    @member_group.command(name="delete", description="Delete a user's stored data and remove the role")
    @officer_only()
    async def delete(self, interaction: discord.Interaction, user: discord.User) -> None:
        await self._forget(interaction, user.id)

    @app_commands.command(name="forget-me", description="Delete your stored data and remove your Verified role")
    @app_commands.guild_only()
    async def forget_me(self, interaction: discord.Interaction) -> None:
        await self._forget(interaction, interaction.user.id)

    async def _forget(self, interaction: discord.Interaction, discord_id: int) -> None:
        deleted = self.app.members.delete(discord_id)
        self.app.verification.abort(discord_id)
        member = interaction.guild.get_member(discord_id)
        if member is not None:
            await set_verified_role(
                interaction.guild, member, self.app.settings.verified_role_id,
                add=False, reason="Data deleted",
            )
        await interaction.response.send_message(
            "Stored data deleted." if deleted else "There was no stored data.", ephemeral=True
        )


async def setup(bot) -> None:
    await bot.add_cog(OfficerCog(bot))
