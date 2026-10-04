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


def test_failed_email_send_lets_the_user_retry_immediately(service):
    begin(service)
    service.send_failed(1)
    begin(service)


def test_abort_does_not_reset_the_cooldown(service):
    """/forget-me calls abort(); it must not become a way around the resend limits."""
    begin(service)
    service.abort(1)
    with pytest.raises(VerificationError, match="seconds"):
        begin(service)


def test_wrong_codes_do_not_reset_the_cooldown(service):
    begin(service)
    for _ in range(5):
        with pytest.raises(VerificationError):
            service.confirm(1, "000000")
    with pytest.raises(VerificationError, match="seconds"):
        begin(service)


def test_expiry_does_not_reset_the_cooldown(service, clock):
    begin(service)
    clock.advance(30)
    with pytest.raises(VerificationError):
        service.confirm(1, "000000")
    clock.advance(600)                      # past expiry, but this is a new minute for the cooldown
    begin(service)                          # allowed: more than 60 s since the last send


def test_per_user_hourly_cap(conn, members, clock):
    svc = VerificationService(
        conn, members, clock=clock, code_factory=lambda: "123456",
        per_user_hourly_cap=3, per_email_hourly_cap=99,
    )
    for i in range(3):
        svc.begin(1, "Alice Smith", f"u{i}@tuc.gr")
        clock.advance(61)
    with pytest.raises(VerificationError, match="You have requested"):
        svc.begin(1, "Alice Smith", "u9@tuc.gr")
    clock.advance(3600)
    svc.begin(1, "Alice Smith", "u9@tuc.gr")


def test_per_recipient_cap_stops_inbox_spam_from_many_accounts(conn, members, clock):
    svc = VerificationService(
        conn, members, clock=clock, code_factory=lambda: "123456",
        per_user_hourly_cap=99, per_email_hourly_cap=2,
    )
    svc.begin(1, "Alice Smith", "victim@tuc.gr")
    svc.begin(2, "Bob Jones", "victim@tuc.gr")
    with pytest.raises(VerificationError, match="That address"):
        svc.begin(3, "Eve Mallory", "Victim@TUC.gr")      # same address, different case and account


def test_one_account_looping_cannot_take_verification_offline(service, clock):
    """Reviewer probe: begin, 5 wrong codes, repeat, as fast as the clock lets it."""
    sent = 0
    for _ in range(600):
        try:
            begin(service, discord_id=1, email="prof.victim@tuc.gr")
            sent += 1
        except VerificationError:
            pass
        for _ in range(5):
            try:
                service.confirm(1, "000000")
            except VerificationError:
                pass
        clock.advance(1)
    assert sent <= 5                                          # per-user hourly cap (default)
    begin(service, discord_id=2, email="honest@tuc.gr")       # a real student can still verify


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


def test_kelvin_sign_look_alike_is_rejected_in_the_real_flow(service):
    with pytest.raises(VerificationError, match="tuc.gr"):
        begin(service, email="\u212aostas@tuc.gr")        # lowercases to ASCII "kostas" if normalised first
