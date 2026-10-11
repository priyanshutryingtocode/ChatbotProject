from orderbot.database import format_timestamp
from orderbot.order_context import format_database_context
from orderbot.responses import classify_intent, format_order_follow_up


def test_agent_order_context_formats_delivery_timestamp_in_ist():
    context = format_database_context(
        {
            "matched": [
                {
                    "public_order_id": 14,
                    "status": "Out for Delivery",
                    "shipments": {
                        "estimated_delivery_at": "2026-05-25T18:45:00Z",
                    },
                }
            ]
        },
        fields={"delivery"},
    )

    assert "Estimated delivery: May 26, 2026 at 12:15 AM IST" in context
    assert "2026-05-25T18:45:00Z" not in context
    assert "UTC" not in context


def test_timestamp_without_timezone_is_treated_as_utc():
    assert format_timestamp("2026-05-25T18:45:00") == "May 26, 2026 at 12:15 AM IST"


def test_delivery_nonreceipt_followup_is_deterministic_and_does_not_invent_support_actions():
    order = {
        "public_order_id": 13,
        "status": "Delivered",
        "shipments": {"delivered_at": "2026-07-08T21:39:00Z"},
    }

    for message in ("i havent received it", "but i didnt get it"):
        assert classify_intent(message) == "delivery_issue"
        answer = format_order_follow_up([order], name_only=False, user_input=message)
        assert "Jul 9, 2026 at 3:09 AM IST" in answer
        assert "contact customer support" in answer
        assert "checked with household members" not in answer
        assert "initiate a thorough investigation" not in answer
