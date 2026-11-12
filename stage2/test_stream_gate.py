import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
import a2a_server

class StreamGateTests(unittest.TestCase):
    def test_stream_returns_explicit_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv, _ = a2a_server.serve_in_thread(Path(tmp), None, 0)
            try:
                url = 'http://127.0.0.1:%d/tasks/t/events' % srv.server_address[1]
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(url, timeout=5)
                self.assertEqual(caught.exception.code, 503)
                self.assertEqual(json.loads(caught.exception.read())['error'],
                                 'stream_disabled_pending_durability_repair')
            finally:
                srv.shutdown()
                srv.server_close()

if __name__ == '__main__':
    unittest.main()
