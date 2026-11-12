import asyncio
import copy
import importlib.util
import json
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HUB_DIRS = (ROOT / "prototype", ROOT / "adapters" / "verification-seat")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Message:
    def __init__(self, envelope):
        self.data = json.dumps(envelope).encode()
        self.reply = "test.reply"
        self.response = None

    async def respond(self, data):
        self.response = json.loads(data)


def envelope():
    return {
        "contract_version": "0.2", "envelope_id": str(uuid.uuid4()),
        "task_id": str(uuid.uuid4()), "trace_id": str(uuid.uuid4()),
        "created_at": "2026-01-01T00:00:00Z", "created_by": "tester",
        "idempotency_key": "same-key", "target": {"capability": "review"},
        "instruction": "inspect", "skill_ref": None, "resource_key": None,
        "params": {"nested": {"trace_id": "business-id", "value": 1}},
        "attachments": [], "timeout_s": 30, "cls": "readonly",
    }


class HubIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_and_conflicts_before_and_after_replay(self):
        # Stub the absent broker package only; validation, handler and ledger are real.
        nats = types.ModuleType("nats")
        aio = types.ModuleType("nats.aio")
        client = types.ModuleType("nats.aio.client")
        client.Client = type("Client", (), {})
        msg_module = types.ModuleType("nats.aio.msg")
        msg_module.Msg = Message
        modules = {"nats": nats, "nats.aio": aio, "nats.aio.client": client,
                   "nats.aio.msg": msg_module}
        with patch.dict(sys.modules, modules):
            for index, directory in enumerate(HUB_DIRS):
                with self.subTest(hub=str(directory)), tempfile.TemporaryDirectory() as tmp:
                    core = load_module("core", directory / "core.py")
                    hub_module = load_module(f"test_hub_{index}", directory / "hub.py")
                    with patch.dict(sys.modules, {"core": core}):
                        hub = hub_module.Hub(Path(tmp), "nats://unused")
                        original = envelope()
                        first = await self.submit(hub, original)
                        self.assertTrue(first["accepted"])
                        for replay in (False, True):
                            if replay:
                                hub = hub_module.Hub(Path(tmp), "nats://unused")
                            retry = copy.deepcopy(original)
                            retry.update(task_id=str(uuid.uuid4()), envelope_id=str(uuid.uuid4()),
                                         trace_id=str(uuid.uuid4()), created_at="2027-01-01T00:00:00Z")
                            response = await self.submit(hub, retry)
                            self.assertTrue(response["duplicate"])
                            self.assertEqual(response["task_id"], original["task_id"])
                            for field, value in (("target", {"capability": "other"}),
                                                 ("cls", "reversible"),
                                                 ("params", {"nested": {"trace_id": "changed", "value": 1}}),
                                                 ("timeout_s", 31)):
                                changed = copy.deepcopy(retry)
                                changed[field] = value
                                response = await self.submit(hub, changed)
                                self.assertFalse(response["ok"], (replay, field, response))
                                self.assertEqual(response["reason"], "idempotency_conflict")
                            self.assertEqual(len(hub.tasks), 1)

    async def submit(self, hub, value):
        msg = Message(value)
        await hub.on_submit(msg)
        await asyncio.sleep(0)
        self.assertIsNotNone(msg.response)
        return msg.response


if __name__ == "__main__":
    unittest.main()
