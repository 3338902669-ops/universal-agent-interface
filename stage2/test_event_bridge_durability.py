import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from event_bridge import EventBridge


class DurabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.ledger = self.base / 'data' / 'ledger.jsonl'
        self.ledger.parent.mkdir()
        self.ledger.touch()
        self.out = self.base / 'data' / 'out.jsonl'
        self.bridge = EventBridge(self.base, self.out, experimental_test_only=True)

    def tearDown(self):
        self.bridge.close()
        self.tmp.cleanup()

    def append(self, *records):
        with self.ledger.open('a', encoding='utf-8') as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')

    def flush(self):
        events = self.bridge.drain()
        self.bridge.write(events)
        return events

    def restart(self):
        self.bridge.close()
        self.bridge = EventBridge(self.base, self.out, experimental_test_only=True)

    def test_delayed_pointer_survives_restart(self):
        self.append({'record': 'TASK_EVENT', 'task_id': 'a', 'seq': 1, 'kind': 'result_ready'})
        self.assertEqual(self.flush(), [])
        self.restart()
        self.append({'record': 'TASK_STATE', 'task_id': 'a', 'state': 'RUNNING', 'result_pointer': {'uri': 'file:///result'}})
        events = self.flush()
        self.assertEqual([e['artifact']['parts'][0]['url'] for e in events if e['kind'] == 'artifact_update'], ['file:///result'])

    def test_state_before_completed_does_not_duplicate_final(self):
        self.append({'record': 'TASK_STATE', 'task_id': 'a', 'state': 'SUCCEEDED'})
        self.flush()
        self.restart()
        self.append({'record': 'TASK_EVENT', 'task_id': 'a', 'seq': 2, 'kind': 'completed'})
        self.flush()
        events = [json.loads(x) for x in self.out.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(sum(bool(e.get('final')) for e in events), 1)

    def test_verdict_order_and_repeated_values_across_restart(self):
        self.append({'record': 'TASK_STATE', 'task_id': 'a', 'state': 'SUCCEEDED'},
                    {'record': 'VERDICT', 'task_id': 'a', 'verdict': '通过'},
                    {'record': 'VERDICT', 'task_id': 'a', 'verdict': '不通过'},
                    {'record': 'VERDICT', 'task_id': 'a', 'verdict': '通过'})
        self.flush()
        self.restart()
        self.assertEqual(self.flush(), [])
        events = [json.loads(x) for x in self.out.read_text(encoding='utf-8').splitlines()]
        texts = [e['status']['message']['parts'][0]['text'] for e in events if e.get('final')]
        self.assertEqual([x for x in texts if x != '待核验'], ['通过', '不通过', '通过'])

    def test_partial_ledger_line_is_retried(self):
        with self.ledger.open('a', encoding='utf-8') as f:
            f.write('{"record":"TASK_EVENT","task_id":"a",')
        self.assertEqual(self.flush(), [])
        with self.ledger.open('a', encoding='utf-8') as f:
            f.write('"seq":1,"kind":"accepted"}\n')
        self.assertEqual([e['kind'] for e in self.flush()], ['task'])

    def test_state_verdict_then_completed_only_one_final_after_restart(self):
        self.append({'record': 'TASK_STATE', 'task_id': 'a', 'state': 'SUCCEEDED'},
                    {'record': 'VERDICT', 'task_id': 'a', 'verdict': '通过'},
                    {'record': 'TASK_EVENT', 'task_id': 'a', 'seq': 1, 'kind': 'completed'})
        events = self.flush()
        finals = [e for e in events if e.get('final')]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].get('uai/seq'), 1)
        self.restart()
        self.assertEqual(self.flush(), [])

    def test_unhashable_result_sequence_does_not_block_replay(self):
        for seq in ([], {}):
            with self.subTest(seq=seq):
                self.bridge.close()
                self.ledger.write_text('', encoding='utf-8')
                self.out.write_text('', encoding='utf-8')
                self.bridge = EventBridge(self.base, self.out, experimental_test_only=True)
                self.append({'record': 'TASK_EVENT', 'task_id': 'a', 'seq': seq, 'kind': 'result_ready'},
                            {'record': 'TASK_EVENT', 'task_id': 'b', 'seq': 1, 'kind': 'accepted'})
                events = self.flush()
                self.assertEqual([e['kind'] for e in events], ['bridge_error', 'task'])
                self.restart()
                self.append({'record': 'TASK_EVENT', 'task_id': 'b', 'seq': 2, 'kind': 'progress'})
                self.assertEqual([e['kind'] for e in self.flush()], ['status_update'])

    def test_string_seq_reports_error_and_does_not_suppress_terminal(self):
        self.append({'record': 'TASK_STATE', 'task_id': 'a', 'state': 'SUCCEEDED'},
                    {'record': 'TASK_EVENT', 'task_id': 'a', 'seq': '1', 'kind': 'completed'})
        events = self.flush()
        self.assertEqual(len([e for e in events if e.get('final')]), 1)
        self.assertTrue(any(e.get('kind') == 'bridge_error' and 'invalid_seq' in e.get('error', '') for e in events))
        self.restart()
        self.assertEqual(self.flush(), [])

    def test_pending_state_then_completed_prefers_real_event_after_restart(self):
        self.append({'record': 'TASK_EVENT', 'task_id': 'a', 'seq': 1, 'kind': 'result_ready'},
                    {'record': 'TASK_STATE', 'task_id': 'a', 'state': 'SUCCEEDED'},
                    {'record': 'TASK_EVENT', 'task_id': 'a', 'seq': 2, 'kind': 'completed'})
        self.assertEqual(self.flush(), [])
        self.restart()
        self.assertEqual(self.flush(), [])
        self.append({'record': 'TASK_STATE', 'task_id': 'a', 'state': 'SUCCEEDED', 'result_pointer': {'uri': 'file:///result'}})
        events = self.flush()
        finals = [e for e in events if e.get('final')]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].get('uai/seq'), 2)
        self.restart()
        self.assertEqual(self.flush(), [])

    def test_duplicate_sequence_does_not_suppress_terminal_state_after_restart(self):
        self.append({'record': 'TASK_EVENT', 'task_id': 'a', 'seq': 1, 'kind': 'accepted'})
        self.flush()
        self.restart()
        self.append({'record': 'TASK_STATE', 'task_id': 'a', 'state': 'SUCCEEDED'},
                    {'record': 'TASK_EVENT', 'task_id': 'a', 'seq': 1, 'kind': 'completed'})
        events = self.flush()
        self.assertEqual(len([e for e in events if e.get('final')]), 1)
        self.restart()
        self.assertEqual(self.flush(), [])

    def test_pending_result_blocks_later_task_events_across_restart(self):
        self.append({'record': 'TASK_EVENT', 'task_id': 'a', 'seq': 1, 'kind': 'result_ready'},
                    {'record': 'TASK_EVENT', 'task_id': 'a', 'seq': 2, 'kind': 'completed'})
        self.assertEqual(self.flush(), [])
        self.restart()
        self.append({'record': 'TASK_STATE', 'task_id': 'a', 'state': 'SUCCEEDED', 'result_pointer': {'uri': 'file:///result'}})
        events = self.flush()
        self.assertEqual([e['kind'] for e in events], ['artifact_update', 'status_update'])
        self.assertEqual([e.get('uai/seq') for e in events], [1, 2])
        self.restart()
        self.assertEqual(self.flush(), [])

    def test_timeout_not_suppressed_by_bad_completed_across_restart(self):
        for event in ({'record': 'TASK_EVENT', 'task_id': 'a', 'kind': 'completed'},
                      {'record': 'TASK_EVENT', 'task_id': 'a', 'seq': 1, 'kind': 'completed'}):
            with self.subTest(event=event):
                self.bridge.close()
                self.ledger.write_text('', encoding='utf-8')
                self.out.write_text('', encoding='utf-8')
                self.bridge = EventBridge(self.base, self.out, experimental_test_only=True)
                self.append({'record': 'TASK_STATE', 'task_id': 'a', 'state': 'TIMED_OUT'}, event)
                events = self.flush()
                finals = [e for e in events if e.get('final')]
                self.assertEqual([e['status']['message']['parts'][0]['text'] for e in finals], ['timed_out'])
                self.restart()
                self.assertEqual(self.flush(), [])

    def test_completed_advances_running_state_for_later_verdict_after_restart(self):
        self.append({'record': 'TASK_STATE', 'task_id': 'a', 'state': 'RUNNING'},
                    {'record': 'TASK_EVENT', 'task_id': 'a', 'seq': 1, 'kind': 'completed'},
                    {'record': 'VERDICT', 'task_id': 'a', 'verdict': '通过'})
        events = self.flush()
        statuses = [e['status']['state'] for e in events if e.get('kind') == 'status_update']
        self.assertEqual(statuses, ['TASK_STATE_COMPLETED', 'TASK_STATE_COMPLETED'])
        self.restart()
        self.assertEqual(self.flush(), [])
        self.append({'record': 'VERDICT', 'task_id': 'a', 'verdict': '不通过'})
        events = self.flush()
        self.assertEqual([e['status']['state'] for e in events], ['TASK_STATE_COMPLETED'])

    def test_running_verdict_does_not_block_completed_across_restart(self):
        self.append({'record': 'TASK_STATE', 'task_id': 'a', 'state': 'RUNNING'},
                    {'record': 'VERDICT', 'task_id': 'a', 'verdict': '通过'})
        events = self.flush()
        self.assertFalse(any(e.get('final') for e in events))
        self.restart()
        self.append({'record': 'TASK_EVENT', 'task_id': 'a', 'seq': 2, 'kind': 'completed'})
        events = self.flush()
        self.assertEqual(sum(bool(e.get('final')) for e in events), 1)
        self.restart()
        self.assertEqual(self.flush(), [])

    def test_incomplete_outbox_fails_closed_on_restart(self):
        self.bridge.close()
        self.out.write_text('{"kind":', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'incomplete_outbox_line'):
            EventBridge(self.base, self.out, experimental_test_only=True)

    def test_completed_then_multiple_verdicts_without_state(self):
        self.append({'record': 'TASK_EVENT', 'task_id': 'a', 'seq': 1, 'kind': 'completed'},
                    {'record': 'VERDICT', 'task_id': 'a', 'verdict': '通过'},
                    {'record': 'VERDICT', 'task_id': 'a', 'verdict': '不通过'})
        events = self.flush()
        texts = [e['status']['message']['parts'][0]['text'] for e in events if e.get('final')]
        self.assertEqual(texts, ['待核验', '通过', '不通过'])
        self.restart()
        self.assertEqual(self.flush(), [])

    def test_write_failure_can_retry_in_same_process(self):
        self.append({'record': 'TASK_EVENT', 'task_id': 'a', 'seq': 1, 'kind': 'accepted'})
        events = self.bridge.drain()
        with patch.object(Path, 'open', side_effect=OSError('write failed')):
            with self.assertRaises(OSError):
                self.bridge.write(events)
        self.assertEqual([e['kind'] for e in self.flush()], ['task'])

    def test_write_failure_can_retry_after_restart(self):
        self.append({'record': 'TASK_EVENT', 'task_id': 'a', 'seq': 1, 'kind': 'accepted'})
        events = self.bridge.drain()
        with patch.object(Path, 'open', side_effect=OSError('write failed')):
            with self.assertRaises(OSError):
                self.bridge.write(events)
        self.restart()
        self.assertEqual([e['kind'] for e in self.flush()], ['task'])


if __name__ == '__main__':
    unittest.main()
