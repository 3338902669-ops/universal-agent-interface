import json
import tempfile
import unittest
from pathlib import Path
import ledger_chain as chain

class IntegrityTests(unittest.TestCase):
    def test_injected_unsealed_record_and_append_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ledger'
            chain.append_sealed(path, {'value': 1})
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'value': 2}) + '\n')
            result = chain.verify_chain(path)
            self.assertFalse(result['ok'])
            self.assertIn((2, 'unsealed_after_chain_start'), result['broken'])
            with self.assertRaisesRegex(ValueError, 'invalid chain'):
                chain.append_sealed(path, {'value': 3})

if __name__ == '__main__':
    unittest.main()
