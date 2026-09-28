"""Shared fixtures.

Every test in this suite runs WITHOUT a Gemini API key. Anything that would
call the model is either driven directly with fixed inputs or skipped, so CI
never needs a secret and a contributor can run the suite on a fresh clone.
"""

import importlib.util
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _NoNetworkClient:
    """Stands in for google.genai.Client for the whole suite.

    A test that forgets to stub a model call used to reach the REAL API
    whenever this machine had a key in gemini_api_key.txt -- quietly, slowly,
    and billed. Every escalation test did exactly that once handoff_message()
    existed. With this in place such a test fails fast instead: the call raises
    here, the coach turns it into ModelUnavailable, and whatever fallback the
    app has is what the test sees.
    """

    def __init__(self, *args, **kwargs):
        self.models = self

    def generate_content(self, *args, **kwargs):
        raise RuntimeError("the test suite makes no network calls; stub this")

    embed_content = generate_content

    def list(self, *args, **kwargs):
        return []


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """Block the real Gemini client everywhere -- except the two semantic
    search tests, which opt in explicitly with GEMINI_API_KEY set."""
    if request.node.get_closest_marker("live_api") and os.getenv("GEMINI_API_KEY"):
        return
    # A placeholder key, so nothing goes looking for a real one. Without it
    # the lookup falls through to the developer's gemini_api_key.txt -- which
    # is how this suite passed locally and failed in CI -- and, on a machine
    # with no key file, to a getpass prompt that has no terminal to read.
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    import coach_core
    monkeypatch.setattr(coach_core.genai, "Client", _NoNetworkClient)
    # A client built before the patch -- the embeddings one is cached.
    monkeypatch.setattr(coach_core, "_embed_client", None, raising=False)


@pytest.fixture(scope="session")
def core():
    """The coaching engine, generated from the notebook."""
    import coach_core
    return coach_core


@pytest.fixture
def srv(tmp_path):
    """A fresh server module with its store pointed at a temp database.

    Loaded per test so module-level state (the live session, the store path)
    never leaks between tests.
    """
    module = _load("srv_under_test", os.path.join(APP, "server.py"))
    module.CASES_DB = str(tmp_path / "cases.db")
    module.CASES_FILE = str(tmp_path / "cases.json")
    module.init_db()
    # The portal answers customers on a worker thread. Here it runs inline,
    # so a test can assert on the reply the moment the request returns
    # instead of racing a thread. tests/test_async_reply.py turns it off.
    module.app.config["AI_INLINE"] = True
    return module


@pytest.fixture
def now():
    return datetime.now(UTC)


@pytest.fixture
def ago(now):
    """ago(minutes=20) -> an ISO timestamp 20 minutes in the past."""
    def _ago(**kwargs):
        return (now - timedelta(**kwargs)).isoformat(timespec="seconds")
    return _ago


@pytest.fixture
def case():
    """Build a case dict without repeating the boilerplate in every test."""
    def _case(**overrides):
        base = {
            "id": "SC-1001",
            "status": "pending",
            "escalation_risk": "medium",
            "sentiment": "negative",
            "opened_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "closed_at": None,
            "turns": 1,
            "calls": 1,
            "messages": [],
        }
        base.update(overrides)
        return base
    return _case


@pytest.fixture
def back_office(core, monkeypatch):
    """An in-memory order system.

    The back-office tools read and WRITE orders.json. Pointed at the real file
    they would both depend on its current contents and mutate them -- so one
    contributor issuing a refund in the app would start breaking the suite.
    """
    data = {
        "account": {"name": "Test User", "email": "test@example.com",
                    "password_reset_sent_at": None},
        "orders": [
            {"order_id": "OD-4468", "item": "Phone case", "amount": 349,
             "currency": "INR", "placed_on": "2026-08-27", "status": "in transit",
             "courier": "BlueDart", "tracking_number": "BD88213004",
             "expected_on": "2026-09-03"},
            {"order_id": "OD-4471", "item": "Prepaid recharge", "amount": 499,
             "currency": "INR", "placed_on": "2026-08-30", "status": "failed"},
        ],
        "refunds": [
            {"refund_id": "RF-9012", "order_id": "OD-4471", "amount": 499,
             "currency": "INR", "status": "processing",
             "initiated_on": "2026-08-31", "expected_by": "2026-09-05",
             "method": "UPI"},
        ],
    }

    def _save(new):
        # issue_refund() hands back the very dict load_orders() gave it, so
        # clearing first would wipe the thing we are about to copy from.
        snapshot = dict(new)
        data.clear()
        data.update(snapshot)
        return True

    monkeypatch.setattr(core, "load_orders", lambda: data)
    monkeypatch.setattr(core, "save_orders", _save)
    return data
