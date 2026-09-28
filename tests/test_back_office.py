"""The mock order system the coach is allowed to query."""

import pytest


def test_looks_up_a_known_order(core, back_office):
    order = core.check_order_status("OD-4468")
    assert order["status"] == "in transit"
    assert order["courier"] == "BlueDart"


def test_unknown_order_returns_an_error_not_an_exception(core, back_office):
    assert "error" in core.check_order_status("OD-0000")


def test_no_id_lists_recent_orders(core, back_office):
    """The customer rarely quotes an order number, so the model is told to call
    this with no arguments first."""
    result = core.check_order_status()
    assert "recent_orders" in result
    assert len(result["recent_orders"]) >= 1


def test_refund_lookup_by_order(core, back_office):
    assert core.check_refund_status(order_id="OD-4471")["refund_id"] == "RF-9012"


def test_refund_lookup_for_an_order_without_one(core, back_office):
    assert "error" in core.check_refund_status(order_id="OD-4468")


def test_duplicate_refund_is_refused(core, back_office):
    result = core.initiate_refund("OD-4471")
    assert "error" in result
    assert "already exists" in result["error"]


def test_reset_rejects_a_customer_who_is_not_on_the_account(core, back_office):
    back_office["account"]["customer_id"] = "CU-1001"
    assert "error" in core.reset_account_access("CU-9999")


@pytest.mark.parametrize("name", [
    "check_order_status", "check_refund_status",
    "initiate_refund", "expedite_delivery", "reset_account_access",
])
def test_every_tool_is_declared_to_the_model(core, name):
    assert name in core.BACK_OFFICE
    assert core.BACK_OFFICE[name]["declaration"].name == name


def test_only_data_changing_tools_are_marked_as_writes(core):
    writes = {k for k, v in core.BACK_OFFICE.items() if v["writes"]}
    assert writes == {"initiate_refund", "expedite_delivery",
                      "reset_account_access"}
    assert writes == core.WRITE_TOOLS


# --------------------------------------------------------------------------
# The three write tools, run directly -- which is what clicking Approve does
# --------------------------------------------------------------------------
def test_initiating_a_refund_creates_one(core, back_office):
    before = len(back_office["refunds"])
    done = core.initiate_refund("OD-4468", reason="never arrived")

    assert done["action"] == "initiate_refund"
    assert done["reference"].startswith("RF-")
    assert done["at"]
    assert done["refund"]["order_id"] == "OD-4468"
    assert done["refund"]["amount"] == 349
    assert len(back_office["refunds"]) == before + 1


def test_a_refund_can_be_partial(core, back_office):
    done = core.initiate_refund("OD-4468", amount=100)
    assert done["refund"]["amount"] == 100


@pytest.mark.parametrize("amount", [0, -50, 9999])
def test_a_refund_outside_the_order_total_is_refused(core, back_office, amount):
    """The last thing between a click and the data, so it checks rather than
    trusting whatever the UI sent."""
    before = len(back_office["refunds"])
    assert "error" in core.initiate_refund("OD-4468", amount=amount)
    assert len(back_office["refunds"]) == before


def test_expediting_moves_the_promised_date(core, back_office):
    done = core.expedite_delivery("OD-4468")
    assert done["reference"].startswith("EX-")
    assert done["expected_on"]
    order = next(o for o in back_office["orders"] if o["order_id"] == "OD-4468")
    assert order["expedited"]["reference"] == done["reference"]


def test_expediting_twice_is_refused(core, back_office):
    core.expedite_delivery("OD-4468")
    assert "error" in core.expedite_delivery("OD-4468")


def test_expediting_an_unknown_order_is_an_error_not_an_exception(core, back_office):
    assert "error" in core.expedite_delivery("OD-0000")


def test_resetting_access_stamps_the_account(core, back_office):
    done = core.reset_account_access()
    assert done["reference"].startswith("AR-")
    assert done["sent_to"] == "test@example.com"
    assert back_office["account"]["password_reset_sent_at"] is not None


def test_every_write_tool_returns_a_reference_and_a_timestamp(core, back_office):
    """The panel shows "Refund initiated - RF-9013 - by Rahul at 20:14", so
    every write has to come back with something to put in it."""
    for done in (core.initiate_refund("OD-4468"),
                 core.expedite_delivery("OD-4468"),
                 core.reset_account_access()):
        assert done.get("reference"), done
        assert done.get("at"), done
        assert done.get("summary"), done
