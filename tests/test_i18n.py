"""The customer-side string catalogue.

The English source is written twice on purpose: once in app/static/i18n.js,
which is what the page renders from when it cannot reach the server, and once
in app/server.py, which is what the model is asked to translate. Two copies
can drift, so the drift is what these tests watch. Everything else here
defends the rule that only CHROME is translated -- never a ticket id, never an
amount, and never a word the customer wrote.
"""

import json
import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N_JS = os.path.join(ROOT, "app", "static", "i18n.js")


@pytest.fixture(scope="module")
def js():
    """The catalogues as the browser would see them, read by running the file.

    Parsing the JavaScript with a regex would pass on a file the browser
    cannot load; running it means a syntax error fails the suite too.
    """
    if not shutil_which("node"):
        pytest.skip("node is not installed")
    script = (
        "global.window={location:{search:''},addEventListener(){}};"
        f"require({json.dumps(I18N_JS)});"
        "const SC=global.window.SC;"
        "process.stdout.write(JSON.stringify({en:SC.EN,hi:SC.seedFor('hi')}));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, check=True).stdout
    return json.loads(out)


def shutil_which(name):
    import shutil
    return shutil.which(name)


# ------------------------------------------------------------------ drift

def test_the_two_english_catalogues_have_the_same_keys(srv, js):
    assert set(js["en"]) == set(srv.UI_STRINGS), (
        "app/static/i18n.js and server.UI_STRINGS have drifted; "
        "a key added to one must be added to the other")


def test_the_two_english_catalogues_have_the_same_text(srv, js):
    """Same key, different words, means the model translates one sentence and
    the page renders another."""
    differing = {k: (js["en"][k], srv.UI_STRINGS[k])
                 for k in js["en"] if js["en"][k] != srv.UI_STRINGS.get(k)}
    assert differing == {}


def test_hindi_is_seeded_for_every_key(js):
    """Hindi is meant to switch with no round trip, which only works if it is
    complete. A gap would silently fall back to English mid-page."""
    assert set(js["hi"]) == set(js["en"])


# ------------------------------------------------- placeholders survive

def holes(text):
    return sorted(re.findall(r"\{(\w+)\}", text))


def test_hindi_keeps_every_placeholder(js):
    for key, english in js["en"].items():
        assert holes(english) == holes(js["hi"][key]), key


def test_a_translation_that_drops_a_placeholder_is_refused(srv, monkeypatch):
    """"{n} tickets" coming back as "tickets" would render a count with no
    number. The English is worse to read and better than wrong."""
    monkeypatch.setattr(
        srv.session.coach, "translate_lines",
        lambda lines, note: ["broken, no placeholder here" for _ in lines])
    srv._UI_CACHE.clear()
    out = srv.ui_strings("ta")
    assert out["tickets.many"] == srv.UI_STRINGS["tickets.many"]


def test_a_failed_batch_does_not_cost_the_whole_page(srv, monkeypatch):
    """One bad response should leave 20 strings English, not 91."""
    calls = {"n": 0}

    def flaky(lines, note):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("model said no")
        return ["ಅ" + line for line in lines]     # a Kannada letter

    monkeypatch.setattr(srv.session.coach, "translate_lines", flaky)
    srv._UI_CACHE.clear()
    out = srv.ui_strings("kn")

    english = [k for k in out if out[k] == srv.UI_STRINGS[k]]
    assert 0 < len(english) <= srv.UI_BATCH
    assert len(out) == len(srv.UI_STRINGS)


def test_a_language_is_bought_once(srv, monkeypatch):
    calls = {"n": 0}

    def count(lines, note):
        calls["n"] += 1
        return ["ಅ" + line for line in lines]

    monkeypatch.setattr(srv.session.coach, "translate_lines", count)
    srv._UI_CACHE.clear()
    srv.ui_strings("te")
    first = calls["n"]
    srv.ui_strings("te")
    assert calls["n"] == first


def test_english_and_hindi_never_ask_the_model(srv, monkeypatch):
    """Both are written by hand, so a call for either is money for nothing."""
    def boom(lines, note):
        raise AssertionError("the model was asked for a seeded language")

    monkeypatch.setattr(srv.session.coach, "translate_lines", boom)
    srv._UI_CACHE.clear()
    assert srv.ui_strings("en") == srv.UI_STRINGS
    assert srv.ui_strings("hi") == srv.UI_STRINGS


# --------------------------------------------- what must never be a key

def test_the_catalogue_holds_no_identifiers_amounts_or_dates(srv):
    """A ticket id or an amount in here would eventually be translated. The
    catalogue is chrome only, and this is what keeps it that way."""
    for key, text in srv.UI_STRINGS.items():
        assert not re.search(r"\b(SC|OD|RF|ORD|INV)-\d", text), key
        assert "₹" not in text, key
        assert not re.search(r"\b\d{4}-\d{2}-\d{2}\b", text), key


def test_the_three_customer_states_are_all_keyed(srv):
    """The pills come from the server as English labels and are looked up by
    that text, so a label the catalogue does not know would stay English."""
    for state in srv.CUSTOMER_STATES.values():
        assert "status." + state["label"] in srv.UI_STRINGS


def test_every_category_is_keyed(srv):
    for category in srv.CATEGORIES:
        assert "cat." + category in srv.UI_STRINGS
