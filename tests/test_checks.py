from types import SimpleNamespace

from bot.checks import is_admin, is_officer


def member_with(*role_ids, admin=False):
    import discord

    return SimpleNamespace(
        roles=[SimpleNamespace(id=i) for i in role_ids],
        guild_permissions=discord.Permissions(administrator=admin),
    )


def test_officer_has_the_role():
    assert is_officer(member_with(1, 42), 42) is True


def test_non_officer():
    assert is_officer(member_with(1, 2), 42) is False


def test_user_without_roles_attribute_is_not_officer():
    assert is_officer(SimpleNamespace(), 42) is False


def test_administrators_count_as_officers_without_the_role():
    assert is_officer(member_with(1, admin=True), 42) is True


def test_only_administrators_are_officers_before_the_role_is_set():
    assert is_officer(member_with(42), None) is False
    assert is_officer(member_with(1, admin=True), None) is True


def test_is_admin():
    assert is_admin(member_with(admin=True)) is True
    assert is_admin(member_with(42)) is False
    assert is_admin(SimpleNamespace()) is False
