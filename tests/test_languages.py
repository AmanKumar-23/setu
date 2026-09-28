"""Ten languages, one rule: only the reply is translated.

The value of that rule is not obvious until it breaks. Every group-by in this
app -- the Knowledge section, the work queue, the dashboard's risk split --
reads fields the analyse step produced. If those fields came back in the
customer's language, "recharge failed" would stop grouping the moment two
customers wrote it in two scripts. So analysis stays English, and the reply
is the only thing that travels.
"""

import languages
import pytest

# ---------------------------------------------------------------- the config

def test_every_language_is_fully_described():
    """A half-filled row would crash the picker or the speech code."""
    for code, meta in languages.LANGUAGES.items():
        assert meta["native"], code
        assert meta["english"], code
        # The speech code is what the browser is handed; it must be a real tag.
        assert "-" in meta["speech"], code
        assert meta["speech"].startswith(code + "-"), code


def test_ten_languages_including_english():
    assert len(languages.LANGUAGES) == 10
    assert languages.DEFAULT_LANGUAGE in languages.LANGUAGES


def test_unknown_codes_fall_back_rather_than_raise():
    assert languages.normalise("kl") == languages.DEFAULT_LANGUAGE
    assert languages.normalise(None) == languages.DEFAULT_LANGUAGE
    assert languages.normalise("") == languages.DEFAULT_LANGUAGE
    assert not languages.is_supported("xx")


# ---------------------------------------------------------------- detection

@pytest.mark.parametrize("text,expect", [
    ("मेरा रिचार्ज फेल हो गया", "hi"),
    ("আমার রিচার্জ ব্যর্থ হয়েছে", "bn"),
    ("என் ரீசார்ஜ் தோல்வியடைந்தது", "ta"),
    ("నా రీఛార్జ్ విఫలమైంది", "te"),
    ("મારું રિચાર્જ નિષ્ફળ ગયું", "gu"),
    ("ನನ್ನ ರೀಚಾರ್ಜ್ ವಿಫಲವಾಗಿದೆ", "kn"),
    ("എന്റെ റീചാർജ് പരാജയപ്പെട്ടു", "ml"),
    ("ਮੇਰਾ ਰੀਚਾਰਜ ਅਸਫਲ ਰਿਹਾ", "pa"),
])
def test_detects_each_script(text, expect):
    assert languages.detect_language(text) == expect


def test_plain_english_is_not_a_language_change():
    assert languages.detect_language("my recharge failed") is None


def test_hinglish_is_not_detected_as_hindi():
    """Hinglish is Latin script. Detection cannot see it, and must not guess:
    a wrong guess would offer to switch a customer who never asked."""
    assert languages.detect_language("mera recharge nahi hua, paise cut gaye") is None


def test_a_stray_word_is_not_a_language_change():
    """One borrowed word in an English sentence is not a reason to offer a
    switch, so detection needs more than a character or two to speak up."""
    assert languages.detect_language("my order said नहीं") is None


def test_detection_survives_empty_and_symbols():
    for text in ("", "   ", "12345", "!!!", None):
        assert languages.detect_language(text or "") is None


# ------------------------------------------------------- the model instruction

def test_english_asks_for_nothing():
    """English is the model's default. Sending an instruction for it would be
    tokens spent to change nothing."""
    assert languages.reply_instruction("en") == ""
    assert languages.reply_instruction("bogus") == ""


def test_instruction_names_the_language_and_protects_the_identifiers():
    note = languages.reply_instruction("hi")
    assert "Hindi" in note
    assert languages.native_name("hi") in note
    lowered = note.lower()
    # The three things that must survive a translation intact.
    assert "order" in lowered and "refund" in lowered
    assert "amount" in lowered
    assert "latin" in lowered          # digits stay 499, never ४९९


def test_speech_codes_are_indian_locales():
    """The browser is handed these directly; an en-US voice reading Tamil is
    worse than no voice at all."""
    for code in languages.LANGUAGES:
        assert languages.speech_code(code).endswith("-IN")
