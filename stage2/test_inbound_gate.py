"""Inbound must not reach NATS while identity and old-key migration are unresolved."""
import unittest
from unittest.mock import AsyncMock, patch
import inbound_bridge


class InboundGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_submission_fails_before_connect(self):
        with patch.object(inbound_bridge, '_connect', new_callable=AsyncMock) as connect:
            with self.assertRaisesRegex(inbound_bridge.InboundBridgeError,
                                        'inbound_disabled_pending_identity_and_idempotency_migration'):
                await inbound_bridge.submit(
                    nats_url='nats://127.0.0.1:1',
                    request={'message': {'messageId': 'm1', 'parts': [{'text': 'x'}]}},
                    created_by='caller', target_interface='worker')
            connect.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
