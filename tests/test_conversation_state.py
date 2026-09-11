from conversation.state import VerificationState


def test_verification_state_requires_order_and_secondary_field():
    state = VerificationState(order_id="42")
    assert state.complete is False

    state.email = "jane@example.com"
    assert state.complete is True
    assert state.name_only is False


def test_name_only_state_and_order_change_reset_secondary_fields():
    state = VerificationState(order_id="42", name="Jane Doe")
    assert state.name_only is True

    state.merge({"order_ids": ["99"], "emails": [], "phones": [], "names": []})
    assert state.as_criteria() == {"order_id": "99"}
