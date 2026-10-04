import pytest

from bot.validation import clean_text, is_tuc_email, normalize_email


@pytest.mark.parametrize("email", [
    "student@tuc.gr",
    "Student.Name@TUC.GR",
    "a1@isc.tuc.gr",
    "x@ece.tuc.gr",
    "  padded@tuc.gr  ",
    "first.last+tag@tuc.gr",
])
def test_accepts_tuc_addresses(email):
    assert is_tuc_email(email)


@pytest.mark.parametrize("email", [
    "x@gmail.com",
    "x@evil-tuc.gr",
    "x@tuc.gr.fake.com",
    "x@.tuc.gr",
    "x@tuc.grx",
    "x@@tuc.gr",
    "@tuc.gr",
    "x@tuc",
    "",
    "a@tuc.gr\nBcc: victim@example.com",   # header injection
    "a b@tuc.gr",
    "x@tuс.gr",                       # Cyrillic 'с' look-alike
    "K@tuc.gr",                       # Kelvin sign lowercases to ASCII 'k'
])
def test_rejects_everything_else(email):
    assert not is_tuc_email(email)


def test_normalize_email_lowercases_and_strips():
    assert normalize_email("  A.B@TUC.gr ") == "a.b@tuc.gr"


def test_clean_text_collapses_whitespace_and_strips():
    assert clean_text("  Maria   Papadopoulou \n", min_len=2, max_len=100, label="Name") == "Maria Papadopoulou"


def test_clean_text_accepts_greek():
    assert clean_text("Αντώνης Μουτσάν", min_len=2, max_len=100, label="Name") == "Αντώνης Μουτσάν"


@pytest.mark.parametrize("raw", ["", " ", "A", "x" * 101, "bad\u200bname", "bell\x07"])
def test_clean_text_rejects_bad_input(raw):
    with pytest.raises(ValueError):
        clean_text(raw, min_len=2, max_len=100, label="Name")


def test_error_message_names_the_field():
    with pytest.raises(ValueError, match="Name"):
        clean_text("A", min_len=2, max_len=100, label="Name")
