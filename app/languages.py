"""The languages a customer can be answered in.

One dict, used four ways: the picker's labels, the speech codes for
recognition and playback, script detection, and the instruction handed to the
model. Four copies of a language list drift the moment somebody adds one.

Nothing here decides POLICY -- it does not know that analysis stays English.
That rule lives where the pipeline calls the model.
"""

DEFAULT_LANGUAGE = "en"

# Unicode blocks, for detecting what the customer actually wrote. Cheap, and
# nothing here needs a model call to guess a language.
SCRIPT_RANGES = {
    "devanagari": (0x0900, 0x097F),
    "bengali":    (0x0980, 0x09FF),
    "gurmukhi":   (0x0A00, 0x0A7F),
    "gujarati":   (0x0A80, 0x0AFF),
    "tamil":      (0x0B80, 0x0BFF),
    "telugu":     (0x0C00, 0x0C7F),
    "kannada":    (0x0C80, 0x0CFF),
    "malayalam":  (0x0D00, 0x0D7F),
}

LANGUAGES = {
    "en": {"native": "English",  "english": "English",   "speech": "en-IN",
           "script": None},
    "hi": {"native": "हिंदी",     "english": "Hindi",     "speech": "hi-IN",
           "script": "devanagari"},
    "bn": {"native": "বাংলা",     "english": "Bengali",   "speech": "bn-IN",
           "script": "bengali"},
    "ta": {"native": "தமிழ்",     "english": "Tamil",     "speech": "ta-IN",
           "script": "tamil"},
    "te": {"native": "తెలుగు",    "english": "Telugu",    "speech": "te-IN",
           "script": "telugu"},
    "mr": {"native": "मराठी",     "english": "Marathi",   "speech": "mr-IN",
           "script": "devanagari"},
    "gu": {"native": "ગુજરાતી",   "english": "Gujarati",  "speech": "gu-IN",
           "script": "gujarati"},
    "kn": {"native": "ಕನ್ನಡ",     "english": "Kannada",   "speech": "kn-IN",
           "script": "kannada"},
    "ml": {"native": "മലയാളം",   "english": "Malayalam", "speech": "ml-IN",
           "script": "malayalam"},
    "pa": {"native": "ਪੰਜਾਬੀ",    "english": "Punjabi",   "speech": "pa-IN",
           "script": "gurmukhi"},
}

# Devanagari is written by both Hindi and Marathi, so a script test cannot
# tell them apart. Detection answers "hi" and the customer corrects it once
# from the picker, which is then remembered -- better than guessing Marathi
# at a Hindi speaker, who outnumber them heavily on this kind of service.
SCRIPT_TO_LANGUAGE = {
    "devanagari": "hi",
    "bengali": "bn", "gurmukhi": "pa", "gujarati": "gu",
    "tamil": "ta", "telugu": "te", "kannada": "kn", "malayalam": "ml",
}


def is_supported(code):
    return code in LANGUAGES


def normalise(code):
    """A usable language code, whatever arrived."""
    code = (code or "").strip().lower()
    return code if code in LANGUAGES else DEFAULT_LANGUAGE


def native_name(code):
    return LANGUAGES[normalise(code)]["native"]


def speech_code(code):
    return LANGUAGES[normalise(code)]["speech"]


def detect_language(text):
    """Which language this was written in, or None when it cannot tell.

    Counts characters per script rather than stopping at the first hit: one
    stray character in a long English sentence is not a language change, and
    Hinglish typed in Latin letters is correctly read as "cannot tell".
    """
    if not text:
        return None

    counts = {}
    latin = 0
    for char in str(text):
        point = ord(char)
        if char.isascii() and char.isalpha():
            latin += 1
            continue
        for script, (low, high) in SCRIPT_RANGES.items():
            if low <= point <= high:
                counts[script] = counts.get(script, 0) + 1
                break

    if not counts:
        return None                 # Latin letters: English, or Hinglish

    winner = max(counts, key=counts.get)
    # A handful of characters is a quotation, not the language of the message.
    if counts[winner] < 4:
        return None
    # And a borrowed word inside an English sentence is still an English
    # sentence. The script has to be what the message is mostly written in,
    # or we would offer to switch a customer who never asked.
    if counts[winner] <= latin:
        return None
    return SCRIPT_TO_LANGUAGE.get(winner)


def reply_instruction(code):
    """What to tell the model about the language it must answer in.

    The rules about identifiers and digits live HERE, next to the language
    list, so every caller that asks for a reply gets the same ones.
    """
    code = normalise(code)
    if code == "en":
        return ""

    native = LANGUAGES[code]["native"]
    english = LANGUAGES[code]["english"]
    return (
        f"\nWrite your reply in {english} ({native}).\n"
        f"- Keep every order id, refund id, tracking number, amount and date "
        f"EXACTLY as written. OD-4471 stays OD-4471, RF-9012 stays RF-9012, "
        f"499 stays 499. Never translate or transliterate one.\n"
        f"- Use Latin digits throughout -- 499, never ৪৯৯ or ௪௯௯. Indian "
        f"customers read amounts in Latin digits.\n"
        f"- The customer may write in a mix of {english} and English. Reply "
        f"in {english} in the register they would actually speak, not a "
        f"formal or literary one.\n"
    )
