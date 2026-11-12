import tempfile
import unittest
from pathlib import Path
import event_bridge

class GateTests(unittest.TestCase):
    def test_default_bridge_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, 'event_bridge_disabled'):
                event_bridge.EventBridge(Path(tmp), Path(tmp) / 'out.jsonl')
            self.assertFalse((Path(tmp) / 'out.jsonl').exists())

if __name__ == '__main__':
    unittest.main()
