import json
import shutil
import tempfile
import unittest
from pathlib import Path

import analyze

SOURCE = Path(__file__).parent / 'raw' / 'run-20261111T234041Z-d5cb962a'


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self.tmp.name) / 'run'
        shutil.copytree(SOURCE, self.raw)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_archived_stub(self):
        self.assertEqual(analyze.summarize(self.raw)['n_trials'], 12)

    def test_missing_freeze_rejected(self):
        (self.raw / 'freeze.json').unlink()
        with self.assertRaises(ValueError):
            analyze.summarize(self.raw)

    def test_changed_fixture_hash_rejected(self):
        path = self.raw / 'freeze.json'
        freeze = json.loads(path.read_text(encoding='utf-8'))
        freeze['tasks_sha256'] = '0' * 64
        path.write_text(json.dumps(freeze), encoding='utf-8')
        with self.assertRaises(ValueError):
            analyze.summarize(self.raw)

    def test_incomplete_and_duplicate_trials_rejected(self):
        path = self.raw / 'trials.jsonl'
        lines = path.read_text(encoding='utf-8').splitlines()
        path.write_text('\n'.join(lines[:-1]) + '\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            analyze.summarize(self.raw)
        path.write_text('\n'.join(lines + [lines[0]]) + '\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            analyze.summarize(self.raw)

    def test_inconsistent_channel_status_rejected(self):
        path = self.raw / 'trials.jsonl'
        lines = path.read_text(encoding='utf-8').splitlines()
        item = json.loads(lines[0])
        item['channel_failure'] = True
        item['score'] = None
        item['error_code'] = None
        lines[0] = json.dumps(item)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'error code'):
            analyze.summarize(self.raw)

    def test_tampered_score_rejected(self):
        path = self.raw / 'trials.jsonl'
        lines = path.read_text(encoding='utf-8').splitlines()
        item = json.loads(lines[0])
        item['score'] = 1 - item['score']
        lines[0] = json.dumps(item)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            analyze.summarize(self.raw)


if __name__ == '__main__':
    unittest.main()
