from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)

BRANCHES = (("main", "Main Branch"), ("cs", "CS"), ("ias", "IAS"), ("quantum", "Quantum"))
BRANCH_LABELS = dict(BRANCHES)
NICKNAME_MAX = 32  # Discord's limit
DESCRIPTION_MAX = 100  # Discord's limit for a dropdown option description


async def apply_branch_roles(guild, member, branch_role_ids: dict[str, int], selected: set[str]) -> bool:
    """Give `member` exactly the selected branch roles (other branch roles are removed). False if Discord refused."""
    held = {role.id for role in member.roles}
    to_add = [guild.get_role(rid) for key, rid in branch_role_ids.items() if key in selected and rid not in held]
    to_remove = [guild.get_role(rid) for key, rid in branch_role_ids.items() if key not in selected and rid in held]
    try:
        if to_add := [role for role in to_add if role is not None]:
            await member.add_roles(*to_add, reason="Branch roles chosen by the member")
        if to_remove := [role for role in to_remove if role is not None]:
            await member.remove_roles(*to_remove, reason="Branch roles changed by the member")
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Could not update branch roles for member id %s: %s", getattr(member, "id", "?"), exc)
        return False
    return True


async def set_nickname(member, full_name: str) -> bool:
    """Rename `member` to their verified full name (cut to Discord's limit). False if Discord refused."""
    nick = full_name.strip()[:NICKNAME_MAX]
    if member.nick == nick:
        return True
    try:
        await member.edit(nick=nick, reason="Verified full name")
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Could not set nickname for member id %s: %s", getattr(member, "id", "?"), exc)
        return False
    return True


class BranchSelect(discord.ui.Select):
    def __init__(self, app, member):
        ids = app.server.branch_role_ids
        held = {role.id for role in member.roles}
        descriptions = app.server.branch_descriptions
        options = [
            discord.SelectOption(
                label=BRANCH_LABELS[key], value=key, default=rid in held, description=descriptions.get(key)
            )
            for key, rid in ids.items()
        ]
        super().__init__(
            placeholder="Choose your branch(es)", min_values=1, max_values=len(options), options=options
        )
        self.app = app

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await apply_branch_roles(
            interaction.guild, interaction.user, self.app.server.branch_role_ids, set(self.values)
        )
        if ok:
            chosen = ", ".join(BRANCH_LABELS[key] for key in self.values)
            await interaction.followup.send(
                f"Done. Your branches: **{chosen}**. You can change them any time in the roles channel.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "I couldn't change your roles. Ask an officer to check that my role is above the branch roles.",
                ephemeral=True,
            )


class BranchPickerView(discord.ui.View):
    def __init__(self, app, member):
        super().__init__(timeout=600)
        self.add_item(BranchSelect(app, member))


def picker_for(app, member) -> BranchPickerView | None:
    """The branch picker, or None if /setup branches has not been run."""
    return BranchPickerView(app, member) if app.server.branch_role_ids else None
