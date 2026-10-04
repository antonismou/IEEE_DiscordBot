from types import SimpleNamespace

from bot.checks import is_officer


def member_with(*role_ids):
    return SimpleNamespace(roles=[SimpleNamespace(id=i) for i in role_ids])


def test_officer_has_the_role():
    assert is_officer(member_with(1, 42), 42) is True


def test_non_officer():
    assert is_officer(member_with(1, 2), 42) is False


def test_user_without_roles_attribute_is_not_officer():
    assert is_officer(SimpleNamespace(), 42) is False
