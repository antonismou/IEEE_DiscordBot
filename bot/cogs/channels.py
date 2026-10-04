from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.channels import OFFICER_LOG
from bot.checks import officer_only


class ChannelsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def app(self):
        return self.bot.app

    channel_group = app_commands.Group(name="channel", description="Choose where the bot posts", guild_only=True)

    def topics(self) -> list[str]:
        names = {feed.topic for feed in self.app.settings.feeds if feed.topic}
        names.add(OFFICER_LOG)
        return sorted(names)

    async def _topic_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=topic, value=topic) for topic in self.topics() if current.lower() in topic
        ][:25]

    @channel_group.command(name="set", description="Choose the channel a topic posts to")
    @officer_only()
    async def set_channel(self, interaction: discord.Interaction, topic: str, channel: discord.TextChannel) -> None:
        topics = self.topics()
        if topic not in topics:
            await interaction.response.send_message(
                f"Unknown topic `{topic}`. Choose one of: {', '.join(topics)}.", ephemeral=True
            )
            return
        permissions = channel.permissions_for(interaction.guild.me)
        missing = [
            label for label, allowed in (
                ("View Channel", permissions.view_channel),
                ("Send Messages", permissions.send_messages),
                ("Embed Links", permissions.embed_links),
            ) if not allowed
        ]
        if missing:
            await interaction.response.send_message(
                f"I can't post in {channel.mention}: my role is missing **{', '.join(missing)}** there. "
                "Give it those permissions and run the command again.",
                ephemeral=True,
            )
            return
        self.app.channels.set(topic, channel.id)
        await interaction.response.send_message(f"`{topic}` now posts to {channel.mention}.", ephemeral=True)

    @set_channel.autocomplete("topic")
    async def set_topic_choices(self, interaction: discord.Interaction, current: str):
        return await self._topic_choices(interaction, current)

    @channel_group.command(name="clear", description="Stop a topic from posting")
    @officer_only()
    async def clear_channel(self, interaction: discord.Interaction, topic: str) -> None:
        cleared = self.app.channels.clear(topic)
        await interaction.response.send_message(
            f"`{topic}` cleared." if cleared else f"`{topic}` had no channel set.", ephemeral=True
        )

    @clear_channel.autocomplete("topic")
    async def clear_topic_choices(self, interaction: discord.Interaction, current: str):
        return await self._topic_choices(interaction, current)

    @channel_group.command(name="list", description="Show where each topic posts")
    @officer_only()
    async def list_channels(self, interaction: discord.Interaction) -> None:
        bound = self.app.channels.all()
        lines = []
        for topic in self.topics():
            if topic in bound:
                lines.append(f"`{topic}` -> <#{bound[topic]}>")
            elif topic == OFFICER_LOG:
                lines.append(f"`{topic}` -> **not set** (optional: problems are only logged)")
            else:
                lines.append(f"`{topic}` -> **not set** (its feeds are paused until you set one)")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(ChannelsCog(bot))
