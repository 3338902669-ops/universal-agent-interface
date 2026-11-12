"""Regression tests for inbound identity and retry semantics."""
import unittest

import inbound


class InboundSemanticsTests(unittest.TestCase):
    def request(self, text="hello", subject=None):
        req = {"message": {"messageId": "m-1", "parts": [{"text": text}]}}
        if subject is not None:
            req["subject"] = subject
        return req

    def make(self, request, **kwargs):
        return inbound.envelope_from_a2a(request, created_by="trusted", target_interface="worker", **kwargs)

    def test_same_message_id_keeps_key_when_semantics_change(self):
        original = self.make(self.request())
        changed_text = self.make(self.request("different"))
        changed_target = inbound.envelope_from_a2a(self.request(), created_by="trusted", target_interface="other")
        changed_class = self.make(self.request(), cls="reversible")
        for changed in (changed_text, changed_target, changed_class):
            self.assertEqual(original["idempotency_key"], changed["idempotency_key"])
        self.assertNotEqual(original["target"], changed_target["target"])
        self.assertNotEqual(original["cls"], changed_class["cls"])

    def test_request_claim_cannot_change_identity(self):
        original = self.make(self.request())
        claimed = self.make(self.request(subject="attacker-claim"))
        self.assertEqual(original["idempotency_key"], claimed["idempotency_key"])
        other = inbound.envelope_from_a2a(self.request(subject="trusted"), created_by="other", target_interface="worker")
        self.assertNotEqual(original["idempotency_key"], other["idempotency_key"])

    def test_unambiguous_subject_and_message_pair(self):
        self.assertNotEqual(inbound.derive_idempotency_key("a:b", "c", "", None),
                            inbound.derive_idempotency_key("a", "b:c", "", None))

    def test_invalid_identity_rejected(self):
        for subject, msgid in (("", "m"), ("trusted", ""), ("trusted", 123)):
            with self.subTest(subject=subject, msgid=msgid):
                req = {"message": {"messageId": msgid, "parts": [{"text": "hello"}]}}
                with self.assertRaises(inbound.InboundError):
                    inbound.envelope_from_a2a(req, created_by=subject, target_interface="worker")

    def test_external_requires_approval(self):
        with self.assertRaises(inbound.InboundError):
            self.make(self.request(), cls="external")
        with self.assertRaises(inbound.InboundError):
            self.make(self.request(), cls="external", approval={"credential_id": "unverified"})


if __name__ == "__main__":
    unittest.main()
