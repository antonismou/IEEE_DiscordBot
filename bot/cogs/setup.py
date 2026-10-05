from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.branches import BRANCHES, DESCRIPTION_MAX
from bot.channels import OFFICER_LOG, topics_for
from bot.checks import admin_only
from bot.pins import PIN_LENGTH, is_valid_pin

NO_MENTIONS = discord.AllowedMentions.none()


class SetupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def app(self):
        return self.bot.app

    setup_group = app_commands.Group(
        name="setup", description="First-time setup (administrators)", guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    @setup_group.command(name="roles", description="Choose the Verified and Officer roles for this server")
    @admin_only()
    async def setup_roles(self, interaction: discord.Interaction, verified: discord.Role, officer: discord.Role) -> None:
        for label, role in (("Verified", verified), ("Officer", officer)):
            if role.is_default() or role.managed:
                await interaction.response.send_message(
                    f"The {label} role can't be @everyone or a bot/integration role. Pick a normal role.",
                    ephemeral=True,
                )
                return
        if verified.id == officer.id:
            await interaction.response.send_message(
                "Verified and Officer must be different roles, otherwise every verified member would be an officer.",
                ephemeral=True,
            )
            return
        self.app.server.configure(
            guild_id=interaction.guild_id, verified_role_id=verified.id, officer_role_id=officer.id
        )
        me = interaction.guild.me
        warning = ""
        if not me.guild_permissions.manage_roles or not me.top_role > verified:
            warning = (
                f"\n\n**Warning:** I can't assign {verified.mention} yet. Give my role **Manage Roles** and drag it "
                "**above** the Verified role in Server Settings -> Roles."
            )
        await interaction.response.send_message(
            f"Saved. Verified role: {verified.mention}. Officer role: {officer.mention}. "
            f"Administrators can always use officer commands too.{warning}",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @setup_group.command(name="branches", description="Choose the roles members can pick after verifying")
    @admin_only()
    async def setup_branches(
        self, interaction: discord.Interaction,
        main: discord.Role, cs: discord.Role, ias: discord.Role, quantum: discord.Role,
    ) -> None:
        roles = {"main": main, "cs": cs, "ias": ias, "quantum": quantum}
        if any(r.is_default() or r.managed for r in roles.values()):
            await interaction.response.send_message(
                "A branch role can't be @everyone or a bot/integration role. Pick normal roles.", ephemeral=True
            )
            return
        if len({r.id for r in roles.values()}) != len(roles) or self.app.server.verified_role_id in {
            r.id for r in roles.values()
        }:
            await interaction.response.send_message(
                "The four branch roles must be different from each other and from the Verified role.", ephemeral=True
            )
            return
        self.app.server.configure_branches({key: role.id for key, role in roles.items()})
        me = interaction.guild.me
        warning = ""
        if not me.guild_permissions.manage_roles or any(not me.top_role > r for r in roles.values()):
            warning = (
                "\n\n**Warning:** I can't assign all of these yet. Give my role **Manage Roles** and drag it "
                "**above** them in Server Settings -> Roles."
            )
        await interaction.response.send_message(
            "Saved. After verifying, members choose among: "
            + ", ".join(f"{role.mention}" for role in roles.values()) + f".{warning}",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @setup_group.command(
        name="branch-description", description="Set the short text shown under a branch in the role picker"
    )
    @app_commands.choices(branch=[app_commands.Choice(name=label, value=key) for key, label in BRANCHES])
    @admin_only()
    async def setup_branch_description(
        self, interaction: discord.Interaction, branch: app_commands.Choice[str], text: str | None = None
    ) -> None:
        text = " ".join(text.split()) if text else None
        if text and len(text) > DESCRIPTION_MAX:
            await interaction.response.send_message(
                f"Keep it to {DESCRIPTION_MAX} characters (it is {len(text)}).", ephemeral=True
            )
            return
        self.app.server.set_branch_description(branch.value, text)
        await interaction.response.send_message(
            f"{branch.name}: {text}" if text else f"{branch.name}: description removed.", ephemeral=True
        )

    @setup_group.command(name="branch-pin", description="Set or remove the PIN needed to take a branch role")
    @app_commands.choices(branch=[app_commands.Choice(name=label, value=key) for key, label in BRANCHES])
    @admin_only()
    async def setup_branch_pin(
        self, interaction: discord.Interaction, branch: app_commands.Choice[str], pin: str | None = None
    ) -> None:
        if pin is not None and not is_valid_pin(pin.strip()):
            await interaction.response.send_message(f"The PIN must be exactly {PIN_LENGTH} digits.", ephemeral=True)
            return
        self.app.server.set_branch_pin(branch.value, pin.strip() if pin else None)
        await interaction.response.send_message(
            f"{branch.name}: PIN saved. Share it only with the people who may take this role." if pin
            else f"{branch.name}: PIN removed, anyone verified can take this role.",
            ephemeral=True,
        )

    @setup_group.command(name="status", description="Show what is set up and what is still missing")
    @admin_only()
    async def setup_status(self, interaction: discord.Interaction) -> None:
        server, bound = self.app.server, self.app.channels.all()

        def role(role_id):
            return f"<@&{role_id}>" if role_id else "**not set**"

        lines = [
            f"**Server:** {'set up' if server.configured else '**not set up** (run `/setup roles`)'}",
            f"**Verified role:** {role(server.verified_role_id)}",
            f"**Officer role:** {role(server.officer_role_id)}",
        ]
        branches = server.branch_role_ids
        lines.append(
            "**Branch roles** (`/setup branches`): "
            + (
                ", ".join(
                    f"<@&{rid}>" + (" (PIN)" if key in server.branch_pin_keys() else "")
                    for key, rid in branches.items()
                )
                if branches else "**not set**"
            )
        )
        lines.append("**Channels** (`/channel set`):")
        for topic in topics_for(self.app.settings.feeds):
            where = f"<#{bound[topic]}>" if topic in bound else "**not set**"
            note = " (optional)" if topic == OFFICER_LOG and topic not in bound else ""
            lines.append(f"- `{topic}` -> {where}{note}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS)


async def setup(bot) -> None:
    await bot.add_cog(SetupCog(bot))
