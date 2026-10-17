from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from bot.audit import audit
from bot.branches import DESCRIPTION_MAX
from bot.channels import AUDIT_LOG, OFFICER_LOG, topics_for
from bot.checks import admin_only
from bot.cogs.branches import refresh_roles_panel
from bot.health import last_backup_time
from bot.mailer import MailError
from bot.pins import PIN_LENGTH, is_valid_pin
from bot.serversettings import LABEL_MAX, MAX_BRANCHES, branch_key

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
        await audit(self.app, interaction, f"set the Verified role to <@&{verified.id}> and the Officer role to <@&{officer.id}>")
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

    @setup_group.command(name="branch-add", description="Add a branch role members can pick (or change an existing one)")
    @admin_only()
    async def setup_branch_add(
        self, interaction: discord.Interaction, name: str, role: discord.Role,
        description: str | None = None, pin: str | None = None,
    ) -> None:
        name = " ".join(name.split())
        key = branch_key(name)
        if not key or len(name) > LABEL_MAX:
            await interaction.response.send_message(
                f"Give the branch a name of up to {LABEL_MAX} characters with at least one letter or digit.",
                ephemeral=True,
            )
            return
        description = " ".join(description.split()) if description else None
        if description and len(description) > DESCRIPTION_MAX:
            await interaction.response.send_message(
                f"Keep the description to {DESCRIPTION_MAX} characters (it is {len(description)}).", ephemeral=True
            )
            return
        if pin is not None and not is_valid_pin(pin.strip()):
            await interaction.response.send_message(f"The PIN must be exactly {PIN_LENGTH} digits.", ephemeral=True)
            return
        if role.is_default() or role.managed:
            await interaction.response.send_message(
                "A branch role can't be @everyone or a bot/integration role. Pick a normal role.", ephemeral=True
            )
            return
        server, existing = self.app.server, self.app.server.branch_role_ids
        taken = {rid for k, rid in existing.items() if k != key} | {server.verified_role_id, server.officer_role_id}
        if role.id in taken:
            await interaction.response.send_message(
                "That role is already used by another branch, or is the Verified/Officer role. Pick a different one.",
                ephemeral=True,
            )
            return
        if key not in existing and len(existing) >= MAX_BRANCHES:
            await interaction.response.send_message(
                f"Discord allows at most {MAX_BRANCHES} options in the picker. Remove a branch first.", ephemeral=True
            )
            return
        server.add_branch(key, name, role.id)
        await audit(self.app, interaction, f"set branch **{discord.utils.escape_markdown(name)}** to <@&{role.id}>"
                    + (" with a PIN" if pin else ""))
        if description:
            server.set_branch_description(key, description)
        if pin:
            server.set_branch_pin(key, pin.strip())
        me = interaction.guild.me
        warning = ""
        if not me.guild_permissions.manage_roles or not me.top_role > role:
            warning = (
                f"\n\n**Warning:** I can't assign {role.mention} yet. Give my role **Manage Roles** and drag it "
                "**above** this role in Server Settings -> Roles."
            )
        await interaction.response.send_message(
            f"Saved. **{name}** -> {role.mention}"
            + (" (PIN set)" if pin else "") + f". There are now {len(server.branch_role_ids)} branches.{warning}",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )
        await refresh_roles_panel(self.app, interaction.client)

    async def _branch_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        needle = current.lower()
        return [
            app_commands.Choice(name=label[:100], value=key)
            for key, label in self.app.server.branch_labels.items() if needle in label.lower()
        ][:25]

    async def _known_branch(self, interaction: discord.Interaction, branch: str) -> str | None:
        """The branch's display name, or None after telling the admin it does not exist."""
        label = self.app.server.branch_labels.get(branch)
        if label is None:
            await interaction.response.send_message(
                "I don't know that branch. Pick one from the list, or add it with `/setup branch-add`.",
                ephemeral=True,
            )
        return label

    @setup_group.command(name="branch-remove", description="Stop offering a branch role in the picker")
    @app_commands.autocomplete(branch=_branch_autocomplete)
    @admin_only()
    async def setup_branch_remove(self, interaction: discord.Interaction, branch: str) -> None:
        label = await self._known_branch(interaction, branch)
        if label is None:
            return
        self.app.server.remove_branch(branch)
        await audit(self.app, interaction, f"removed branch **{discord.utils.escape_markdown(label)}**")
        await interaction.response.send_message(
            f"**{label}** removed from the picker, along with its PIN and description. "
            "The Discord role itself, and the members who hold it, are untouched.",
            ephemeral=True,
        )
        await refresh_roles_panel(self.app, interaction.client)

    @setup_group.command(
        name="branch-description", description="Set the short text shown under a branch in the role picker"
    )
    @app_commands.autocomplete(branch=_branch_autocomplete)
    @admin_only()
    async def setup_branch_description(
        self, interaction: discord.Interaction, branch: str, text: str | None = None
    ) -> None:
        label = await self._known_branch(interaction, branch)
        if label is None:
            return
        text = " ".join(text.split()) if text else None
        if text and len(text) > DESCRIPTION_MAX:
            await interaction.response.send_message(
                f"Keep it to {DESCRIPTION_MAX} characters (it is {len(text)}).", ephemeral=True
            )
            return
        self.app.server.set_branch_description(branch, text)
        await interaction.response.send_message(
            f"{label}: {text}" if text else f"{label}: description removed.", ephemeral=True
        )
        await refresh_roles_panel(self.app, interaction.client)

    @setup_group.command(name="branch-pin", description="Set or remove the PIN needed to take a branch role")
    @app_commands.autocomplete(branch=_branch_autocomplete)
    @admin_only()
    async def setup_branch_pin(
        self, interaction: discord.Interaction, branch: str, pin: str | None = None
    ) -> None:
        label = await self._known_branch(interaction, branch)
        if label is None:
            return
        if pin is not None and not is_valid_pin(pin.strip()):
            await interaction.response.send_message(f"The PIN must be exactly {PIN_LENGTH} digits.", ephemeral=True)
            return
        self.app.server.set_branch_pin(branch, pin.strip() if pin else None)
        await audit(
            self.app, interaction,
            f"{'changed' if pin else 'removed'} the PIN of branch **{discord.utils.escape_markdown(label)}**",
        )
        await interaction.response.send_message(
            f"{label}: PIN saved. Share it only with the people who may take this role." if pin
            else f"{label}: PIN removed, anyone verified can take this role.",
            ephemeral=True,
        )
        await refresh_roles_panel(self.app, interaction.client)

    @setup_group.command(name="status", description="Show what is set up and what is still missing")
    @admin_only()
    async def setup_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)  # the mail login test can take a few seconds
        server, bound = self.app.server, self.app.channels.all()

        def role(role_id):
            return f"<@&{role_id}>" if role_id else "**not set**"

        lines = [
            f"**Server:** {'set up' if server.configured else '**not set up** (run `/setup roles`)'}",
            f"**Verified role:** {role(server.verified_role_id)}",
            f"**Officer role:** {role(server.officer_role_id)}",
        ]
        branches, labels, pins = server.branch_role_ids, server.branch_labels, server.branch_pin_keys()
        lines.append(
            "**Branch roles** (`/setup branch-add`): "
            + (
                ", ".join(
                    f"{labels.get(key, key)} -> <@&{rid}>" + (" (PIN)" if key in pins else "")
                    for key, rid in branches.items()
                )
                if branches else "**not set**"
            )
        )
        lines.append("**Channels** (`/channel set`):")
        for topic in topics_for(self.app.settings.feeds):
            where = f"<#{bound[topic]}>" if topic in bound else "**not set**"
            note = " (optional)" if topic == OFFICER_LOG and topic not in bound else ""
            if topic == AUDIT_LOG and topic not in bound:
                note = " (officer actions are not being recorded)"
            lines.append(f"- `{topic}` -> {where}{note}")
        lines.append("**Health:**")
        try:
            await asyncio.to_thread(self.app.check_mail)
            lines.append("- Mail login: OK")
        except MailError as exc:
            lines.append(f"- Mail login: **FAILED** ({type(exc.__cause__ or exc).__name__}). Codes cannot be sent.")
        backup = last_backup_time(self.app.settings.backup_dir)
        lines.append(f"- Last backup: {f'<t:{int(backup.timestamp())}:R>' if backup else '**none yet**'}")
        poll = self.app.health.last_poll
        lines.append(f"- Last feed poll: {f'<t:{int(poll.timestamp())}:R>' if poll else 'not yet since the last restart'}")
        await interaction.followup.send("\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS)


async def setup(bot) -> None:
    await bot.add_cog(SetupCog(bot))
