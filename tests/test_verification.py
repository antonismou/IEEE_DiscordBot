from datetime import datetime, timedelta, timezone

import pytest

from bot.verification import VerificationError, VerificationService


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(conn, members, clock):
    return VerificationService(conn, members, clock=clock, code_factory=lambda: "123456")


def begin(service, discord_id=1, name="Alice Smith", email="alice@tuc.gr"):
    return service.begin(discord_id, name, email)


def test_begin_returns_code_for_the_normalised_email(service):
    pending = begin(service, email=" Alice@ISC.TUC.GR ")
    assert pending.email == "alice@isc.tuc.gr"
    assert pending.code == "123456" and pending.ttl_minutes == 10


def test_plain_code_is_never_stored(service, conn):
    begin(service)
    row = conn.execute("SELECT * FROM pending_codes").fetchone()
    assert "123456" not in tuple(str(v) for v in tuple(row))


@pytest.mark.parametrize("email", ["x@gmail.com", "x@evil-tuc.gr", "x@tuc.gr.fake.com"])
def test_rejects_non_tuc_email(service, email):
    with pytest.raises(VerificationError, match="tuc.gr"):
        begin(service, email=email)


def test_rejects_bad_name(service):
    with pytest.raises(VerificationError, match="Name"):
        begin(service, name="A")


def test_rejects_already_verified_user(service):
    begin(service)
    service.confirm(1, "123456")
    with pytest.raises(VerificationError, match="already verified"):
        begin(service, email="other@tuc.gr")


def test_rejects_email_linked_to_someone_else(service, clock):
    begin(service, discord_id=1)
    service.confirm(1, "123456")
    with pytest.raises(VerificationError, match="already linked"):
        begin(service, discord_id=2)


def test_resend_cooldown(service, clock):
    begin(service)
    clock.advance(30)
    with pytest.raises(VerificationError, match="30 seconds"):
        begin(service)
    clock.advance(31)
    begin(service)


def test_hourly_cap(conn, members, clock):
    service = VerificationService(
        conn, members, clock=clock, code_factory=lambda: "123456", hourly_send_cap=3, resend_cooldown_seconds=0
    )
    for i in range(3):
        service.begin(i + 1, "Alice Smith", f"user{i}@tuc.gr")
    with pytest.raises(VerificationError, match="try again later"):
        service.begin(10, "Alice Smith", "user10@tuc.gr")
    clock.advance(3601)
    service.begin(10, "Alice Smith", "user10@tuc.gr")


def test_abort_removes_pending_so_user_can_retry_immediately(service):
    begin(service)
    service.abort(1)
    begin(service)


def test_confirm_success_creates_member_with_consent_time(service, members, clock):
    begin(service, name="Alice  Smith")
    clock.advance(120)
    member = service.confirm(1, "123456")
    assert member.full_name == "Alice Smith" and member.email == "alice@tuc.gr"
    assert member.method == "email"
    assert member.consent_at == "2026-10-04T12:00:00+00:00"
    assert member.verified_at == "2026-10-04T12:02:00+00:00"
    assert members.get(1) == member


def test_code_with_spaces_is_accepted(service):
    begin(service)
    assert service.confirm(1, " 123 456 ").discord_id == 1


def test_code_cannot_be_replayed_after_success(service):
    begin(service)
    service.confirm(1, "123456")
    with pytest.raises(VerificationError, match="Press"):
        service.confirm(1, "123456")


def test_confirm_without_begin(service):
    with pytest.raises(VerificationError, match="Press"):
        service.confirm(1, "123456")


@pytest.mark.parametrize("bad", ["000000", "abcdef", "", "12345", "1234567"])
def test_wrong_or_malformed_codes_count_as_attempts(service, bad):
    begin(service)
    with pytest.raises(VerificationError, match="4 attempt"):
        service.confirm(1, bad)


def test_fifth_wrong_attempt_invalidates_the_code(service):
    begin(service)
    for _ in range(4):
        with pytest.raises(VerificationError, match="Wrong code"):
            service.confirm(1, "000000")
    with pytest.raises(VerificationError, match="Too many"):
        service.confirm(1, "000000")
    with pytest.raises(VerificationError, match="Press"):
        service.confirm(1, "123456")  # even the right code no longer works


def test_expired_code(service, clock):
    begin(service)
    clock.advance(601)
    with pytest.raises(VerificationError, match="expired"):
        service.confirm(1, "123456")
    with pytest.raises(VerificationError, match="Press"):
        service.confirm(1, "123456")


def test_email_taken_between_begin_and_confirm(service, members, clock):
    begin(service, discord_id=1)
    begin(service, discord_id=2)           # both pending for the same email
    service.confirm(1, "123456")
    with pytest.raises(VerificationError, match="already linked"):
        service.confirm(2, "123456")
    assert members.get(2) is None
