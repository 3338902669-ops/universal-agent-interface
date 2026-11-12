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
        summary = analyze.summarize(self.raw)
        self.assertEqual(summary['n_trials'], 12)
        self.assertAlmostEqual(summary['metrics']['paired_orchestration_delta'], 0.4)
        self.assertEqual(summary['pairing']['n_paired'], 5)
        self.assertAlmostEqual(summary['pairing']['coverage'], 5 / 6)

    def test_asymmetric_failures_report_paired_delta_and_coverage(self):
        path = self.raw / 'trials.jsonl'
        trials = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        for trial in trials:
            if trial['task_id'] == 'T02' and trial['condition'] == 'with_orchestration':
                trial.update(channel_failure=True, score=None, error_code='empty_channel')
        path.write_text(''.join(json.dumps(t) + '\n' for t in trials), encoding='utf-8')
        summary = analyze.summarize(self.raw)
        self.assertAlmostEqual(summary['metrics']['orchestration_delta'], 0.4)
        self.assertEqual(summary['metrics']['paired_orchestration_delta'], 0.25)
        self.assertEqual(summary['pairing']['n_paired'], 4)
        self.assertEqual(summary['pairing']['n_tasks'], 6)
        self.assertAlmostEqual(summary['pairing']['coverage'], 4 / 6)

    def test_no_valid_pairs_have_null_delta_and_zero_coverage(self):
        path = self.raw / 'trials.jsonl'
        trials = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        for trial in trials:
            if trial['condition'] == 'with_orchestration':
                trial.update(channel_failure=True, score=None, error_code='empty_channel')
        path.write_text(''.join(json.dumps(t) + '\n' for t in trials), encoding='utf-8')
        summary = analyze.summarize(self.raw)
        self.assertIsNone(summary['metrics']['paired_orchestration_delta'])
        self.assertEqual(summary['pairing']['n_paired'], 0)
        self.assertEqual(summary['pairing']['coverage'], 0)

    def test_modified_scorer_snapshot_rejected(self):
        (self.raw / 'scoring.frozen.py').write_text('# tampered', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'snapshot hash mismatch'):
            analyze.summarize(self.raw)

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
