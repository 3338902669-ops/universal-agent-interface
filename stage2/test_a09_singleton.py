"""A-09: actual cross-process exclusion and ownership checks."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import tempfile
import time
import unittest
from pathlib import Path

import hub_singleton as HS


def contender(path, start, results, finish):
    guard = HS.HubSingleton(path, namespace="lab", ttl_s=0.01)
    start.wait()
    ok, info = guard.acquire()
    results.put((ok, info))
    finish.wait(10)
    if ok:
        guard.release()


class SingletonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "hub.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_simultaneous_processes_only_one_wins(self):
        ctx = mp.get_context("spawn")
        start, finish, results = ctx.Event(), ctx.Event(), ctx.Queue()
        workers = [ctx.Process(target=contender, args=(str(self.path), start, results, finish)) for _ in range(6)]
        try:
            for worker in workers:
                worker.start()
            start.set()
            outcomes = [results.get(timeout=15) for _ in workers]
            self.assertEqual(sum(ok for ok, _ in outcomes), 1, outcomes)
        finally:
            finish.set()
            for worker in workers:
                worker.join(15)
                if worker.is_alive():
                    worker.terminate()
                    worker.join()
        self.assertTrue(all(worker.exitcode == 0 for worker in workers))

    def test_expired_live_holder_cannot_be_taken_over(self):
        a = HS.HubSingleton(self.path, namespace="lab", ttl_s=0.01)
        self.assertTrue(a.acquire()[0])
        try:
            time.sleep(0.03)
            b = HS.HubSingleton(self.path, namespace="other", ttl_s=0.01)
            self.assertFalse(b.acquire()[0])
        finally:
            a.release()

    def test_expired_holder_release_allows_reacquisition(self):
        a = HS.HubSingleton(self.path, namespace="lab", ttl_s=0.01)
        self.assertTrue(a.acquire()[0])
        time.sleep(0.03)
        with self.assertRaises(HS.SingletonError):
            a.assert_can_dispatch()
        a.release()
        b = HS.HubSingleton(self.path, namespace="lab")
        try:
            self.assertTrue(b.acquire()[0])
        finally:
            b.release()

    def test_replaced_record_revokes_old_holder_and_release_preserves_new(self):
        a = HS.HubSingleton(self.path, namespace="lab")
        self.assertTrue(a.acquire()[0])
        record = json.loads(self.path.read_text(encoding="utf-8"))
        record["generation"] += 1
        self.path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(HS.SingletonError):
            a.assert_can_dispatch()
        with self.assertRaises(HS.SingletonError):
            a.heartbeat()
        a.release()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), record)

    def test_dead_process_takeover_records_event(self):
        ctx = mp.get_context("spawn")
        start, finish, results = ctx.Event(), ctx.Event(), ctx.Queue()
        worker = ctx.Process(target=contender, args=(str(self.path), start, results, finish))
        worker.start()
        start.set()
        self.assertTrue(results.get(timeout=15)[0])
        worker.terminate()
        worker.join(15)
        guard = HS.HubSingleton(self.path, namespace="lab")
        try:
            ok, info = guard.acquire()
            self.assertTrue(ok, info)
            self.assertEqual(info["reason"], "holder_pid_dead")
            self.assertEqual(guard.holder.generation, 2)
            guard.assert_can_dispatch()
            self.assertTrue(guard.heartbeat()["ok"])
        finally:
            guard.release()


if __name__ == "__main__":
    unittest.main()
