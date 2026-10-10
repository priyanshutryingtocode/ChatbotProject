import unittest

from orderbot.privacy import ORDER_REPLY_PLACEHOLDER, VERIFICATION_PLACEHOLDER, persisted_message


class TranscriptPrivacyTests(unittest.TestCase):
    def test_identity_turn_is_not_stored_verbatim(self):
        self.assertEqual(
            persisted_message("user", "Order 42, my email is jane@example.test"),
            VERIFICATION_PLACEHOLDER,
        )

    def test_order_answer_is_replaced_when_order_context_is_present(self):
        self.assertEqual(
            persisted_message("assistant", "Your tracking number is ABC123.", order_context=True),
            ORDER_REPLY_PLACEHOLDER,
        )

    def test_public_text_is_retained_but_contact_details_are_scrubbed(self):
        value = persisted_message("user", "How do returns work? Contact jane@example.test")
        self.assertIn("How do returns work?", value)
        self.assertNotIn("jane@example.test", value)


if __name__ == "__main__":
    unittest.main()
