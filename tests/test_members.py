import csv
import io
from datetime import datetime, timezone

import pytest

from bot.members import EmailAlreadyUsed, members_to_csv

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def add_email(members, discord_id=1, email="a@tuc.gr", name="Alice"):
    return members.add_email_member(
        discord_id=discord_id, email=email, full_name=name, consent_at=NOW, verified_at=NOW
    )


def test_add_and_get(members):
    added = add_email(members)
    assert members.get(1) == added
    assert added.method == "email" and added.email == "a@tuc.gr"
    assert members.get_by_email("a@tuc.gr").discord_id == 1
    assert members.get(99) is None and members.get_by_email("zz@tuc.gr") is None


def test_same_user_can_update_their_record(members):
    add_email(members, name="Alice")
    add_email(members, email="new@tuc.gr", name="Alice B")
    assert members.get(1).full_name == "Alice B"
    assert members.get_by_email("a@tuc.gr") is None


def test_email_cannot_belong_to_two_accounts(members):
    add_email(members, discord_id=1)
    with pytest.raises(EmailAlreadyUsed):
        add_email(members, discord_id=2)
    assert members.get(2) is None


def test_manual_members_have_no_email_and_can_be_many(members):
    m = members.add_manual_member(discord_id=5, full_name="Prof X", verified_by=100, note="Professor", verified_at=NOW)
    members.add_manual_member(discord_id=6, full_name="Alum Y", verified_by=100, note="Alumni", verified_at=NOW)
    assert m.method == "manual" and m.email is None and m.verified_by == 100 and m.note == "Professor"
    assert len(members.all()) == 2


def test_delete(members):
    add_email(members)
    assert members.delete(1) is True
    assert members.delete(1) is False
    assert members.get(1) is None


def test_csv_has_header_and_rows(members):
    add_email(members)
    rows = list(csv.reader(io.StringIO(members_to_csv(members.all()))))
    assert rows[0] == ["discord_id", "email", "full_name", "method", "verified_by", "note", "consent_at", "verified_at"]
    assert rows[1][:4] == ["1", "a@tuc.gr", "Alice", "email"]


@pytest.mark.parametrize("name", ["=HYPERLINK(\"http://x\",\"y\")", "+1+1", "-2+3", "@SUM(A1)"])
def test_csv_neutralises_formulas(members, name):
    members.add_manual_member(discord_id=7, full_name=name, verified_by=1, note="n", verified_at=NOW)
    rows = list(csv.reader(io.StringIO(members_to_csv(members.all()))))
    assert rows[1][2] == "'" + name
