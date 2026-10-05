from __future__ import annotations

import logging

import discord

from bot.pins import PIN_LENGTH

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
        selected = set(self.values)
        held = {role.id for role in interaction.user.roles}
        ids = self.app.server.branch_role_ids
        protected = self.app.server.branch_pin_keys()
        needs_pin = [key for key, _ in BRANCHES if key in selected and key in protected and ids[key] not in held]
        if not needs_pin:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await finish_selection(self.app, interaction, selected)
            return
        wait = self.app.pin_limiter.locked_for(interaction.user.id)
        if wait:
            await interaction.response.send_message(locked_text(wait), ephemeral=True)
            return
        await interaction.response.send_modal(PinModal(self.app, selected, needs_pin))


def locked_text(seconds: int) -> str:
    return f"Too many wrong PINs. Please try again in {-(-seconds // 60)} minute(s)."


async def finish_selection(app, interaction: discord.Interaction, selected: set[str]) -> None:
    """Apply the (already authorised) selection and tell the member. The interaction must be deferred."""
    ok = await apply_branch_roles(interaction.guild, interaction.user, app.server.branch_role_ids, selected)
    if ok:
        chosen = ", ".join(BRANCH_LABELS[key] for key, _ in BRANCHES if key in selected)
        await interaction.followup.send(
            f"Done. Your branches: **{chosen}**. You can change them any time in the roles channel.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            "I couldn't change your roles. Ask an officer to check that my role is above the branch roles.",
            ephemeral=True,
        )


class PinModal(discord.ui.Modal, title="Branch PINs"):
    def __init__(self, app, selected: set[str], needs_pin: list[str]):
        super().__init__()
        self.app, self.selected, self.needs_pin = app, selected, needs_pin
        self.inputs = {}
        for key in needs_pin:
            field = discord.ui.TextInput(
                label=f"PIN for {BRANCH_LABELS[key]}"[:45], min_length=PIN_LENGTH, max_length=PIN_LENGTH,
                placeholder=f"{PIN_LENGTH} digits",
            )
            self.inputs[key] = field
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        limiter, user_id = self.app.pin_limiter, interaction.user.id
        wait = limiter.locked_for(user_id)
        if wait:
            await interaction.followup.send(locked_text(wait), ephemeral=True)
            return
        # check every PIN so that the answer does not reveal which one was wrong
        results = [self.app.server.check_branch_pin(key, str(field).strip()) for key, field in self.inputs.items()]
        if not all(results):
            left = limiter.record_failure(user_id)
            await interaction.followup.send(
                "A PIN was wrong, so no roles were changed. "
                + (f"{left} attempt(s) left." if left else locked_text(limiter.locked_for(user_id))),
                ephemeral=True,
            )
            return
        limiter.record_success(user_id)
        await finish_selection(self.app, interaction, self.selected)


class BranchPickerView(discord.ui.View):
    def __init__(self, app, member):
        super().__init__(timeout=600)
        self.add_item(BranchSelect(app, member))


def picker_for(app, member) -> BranchPickerView | None:
    """The branch picker, or None if /setup branches has not been run."""
    return BranchPickerView(app, member) if app.server.branch_role_ids else None
