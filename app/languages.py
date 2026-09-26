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


# ---------------------------------------------------------------------------
# Reply-only languages
#
# Hinglish is how a great many customers actually write -- "mera recharge
# nahi hua, paise cut gaye". It is not a picker option (the interface has no
# Hinglish to show), but a reply to a Hinglish message should come back in
# Hinglish, not in textbook Hindi and not in English. Spoken with an Indian
# English voice: a Hindi voice reading Latin letters is worse than useless.
# ---------------------------------------------------------------------------
HINGLISH = "hinglish"
REPLY_ONLY = {
    HINGLISH: {"native": "Hinglish", "english": "Hinglish",
               "speech": "en-IN", "script": None},
}


def is_supported(code):
    return code in LANGUAGES


def is_reply_language(code):
    return code in LANGUAGES or code in REPLY_ONLY


def _meta(code):
    return LANGUAGES.get(code) or REPLY_ONLY.get(code) or LANGUAGES[DEFAULT_LANGUAGE]


def normalise(code):
    """A usable language code, whatever arrived."""
    code = (code or "").strip().lower()
    return code if code in LANGUAGES else DEFAULT_LANGUAGE


def native_name(code):
    return _meta(code)["native"]


def speech_code(code):
    return _meta(code)["speech"]


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


# ---------------------------------------------------------------------------
# Per-message detection
# ---------------------------------------------------------------------------

# Romanised Hindi words that are not also common English words. "me", "to",
# "the" and "is" are deliberately absent: each is Hinglish AND English, and
# would turn every English sentence Hinglish.
HINGLISH_WORDS = frozenset(["hai", "hain", "nahi", "nahin", "nhi", "mera", "meri", "mere", "mujhe", "mujhko", "kya", "kyu", "kyun", "kyon", "kab", "kaise", "kaisa", "kaisi", "kitna", "kitne", "kitni", "paisa", "paise", "aur", "gaya", "gayi", "gaye", "hua", "hui", "hue", "raha", "rahi", "rahe", "kar", "karo", "karna", "karke", "kijiye", "kariye", "bhai", "bhaiya", "ji", "abhi", "tak", "wapas", "vapas", "jaldi", "bahut", "bohot", "bahot", "accha", "acha", "achha", "theek", "thik", "lekin", "koi", "kuch", "sab", "bhi", "yeh", "woh", "wo", "hum", "humara", "hamara", "aap", "aapka", "aapki", "apna", "apni", "kal", "aaj", "din", "mil", "mila", "mili", "milega", "milegi", "chahiye", "batao", "bataiye", "bataye", "dijiye", "liye", "wala", "wali", "wale", "sirf", "phir", "fir", "pata", "samajh", "bola", "bolo", "diya", "liya", "kiya", "kiye", "hoga", "hogi", "tha", "thi", "se", "ko", "ka", "ki", "ke", "ghar", "kaam", "hoti", "hota", "karta", "karti", "denge", "dena", "lena", "rakha", "rakho", "hojaye", "hojayega", "gya", "gyi", "hui", "thik", "kripya"])

# Words that tell Marathi from Hindi. Both are written in Devanagari, so the
# script alone cannot; these can.
MARATHI_MARKERS = frozenset(
    ["आहे", "आहेत", "नाही", "माझा", "माझे", "माझी", "मला", "तुम्ही", "झाला", "झाली", "झाले", "आणि", "पण", "काय", "केला", "केले", "केली", "होते", "होता", "होती", "मिळाले", "मिळाला", "अजून", "कधी"])
HINDI_MARKERS = frozenset(
    ["है", "हैं", "नहीं", "मेरा", "मेरी", "मेरे", "मुझे", "हुआ", "हुई", "और", "लेकिन", "क्या", "आप", "किया", "था", "थी", "मिला", "अभी", "तक", "कब", "कैसे"])


# Split on space and punctuation, NOT on \W: Devanagari vowel signs are not
# "word characters" to Python, so \w+ would cut मेरा into म and र.
_PUNCTUATION = ".,!?;:'\"()[]{}<>/\\|@#$%^&*+=~`\u0964\u0965-\u2014\u2019\u201c\u201d"


def _words(text):
    out = []
    for raw in str(text or "").lower().split():
        word = raw.strip(_PUNCTUATION)
        if word and not any(ch.isdigit() for ch in word):
            out.append(word)
    return out


def detect_message_language(text):
    """The language ONE message is written in, or None when it cannot tell.

    Runs on every message, not once per conversation, so a customer who
    switches mid-chat is answered in the language they just used.

    None is a real answer -- "ok", "499", "SC-2004", a single "thanks" -- and
    the caller keeps the conversation's last language rather than guessing.
    """
    by_script = detect_language(text)
    if by_script in ("hi", "mr"):
        words = set(_words(text))
        marathi = len(words & MARATHI_MARKERS)
        hindi = len(words & HINDI_MARKERS)
        if marathi > hindi:
            return "mr"
        if hindi > marathi:
            return "hi"
        return "devanagari"            # one script, two languages: caller decides
    if by_script:
        return by_script

    words = [w for w in _words(text) if w.isascii()]
    if not words:
        return None
    hinglish = sum(1 for w in words if w in HINGLISH_WORDS)
    if hinglish >= 2 and hinglish / len(words) >= 0.2:
        return HINGLISH
    # English needs a little evidence: a lone "thanks" or "ok" should not
    # flip a Hindi conversation into English.
    if len(words) >= 2 or sum(len(w) for w in words) >= 12:
        return "en"
    return None


def reply_language(text, previous=None, preferred=None):
    """Which language to answer THIS message in, and how we decided.

    Returns (code, how). `how` is kept on the message so an agent can see
    whether the language was read from what the customer wrote or carried
    over because that message gave nothing to go on.
    """
    previous = previous if is_reply_language(previous or "") else None
    preferred = preferred if is_supported(preferred or "") else None

    found = detect_message_language(text)
    if found == "devanagari":
        # Hindi or Marathi, and the words did not settle it. Whichever of the
        # two this conversation was already in wins; then the customer's
        # chosen language; then Hindi, the far larger of the two.
        for candidate in (previous, preferred):
            if candidate in ("hi", "mr"):
                return candidate, "detected"
        return "hi", "detected"
    if found:
        return found, "detected"
    if previous:
        return previous, "kept"
    if preferred:
        return preferred, "preferred"
    return DEFAULT_LANGUAGE, "default"


def reply_instruction(code):
    """What to tell the model about the language it must answer in.

    The rules about identifiers and digits live HERE, next to the language
    list, so every caller that asks for a reply gets the same ones.
    """
    if code == HINGLISH:
        return (
            "\nThe customer writes in Hinglish: Hindi, typed in Latin letters. "
            "Reply the same way -- Hinglish in Latin letters, the way they wrote "
            "it. Do NOT switch to Devanagari, and do not answer in formal "
            "English.\n"
            "- Keep every order id, refund id, tracking number, amount and date "
            "EXACTLY as written. OD-4471 stays OD-4471, 499 stays 499.\n"
        )

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
