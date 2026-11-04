# Stage 1 conformance

Install the test dependency and run the suite from the package root:

```text
python -m pip install -r tests/conformance/requirements.txt
python tests/conformance/run_conformance.py
```

The suite validates the shipped examples against the JSON schemas, checks the canonical verdict enum, verifies the read-only historical alias normalization, and asserts that the alias is rejected at an output boundary. A successful run ends with `SUMMARY <passed>/<total> PASS` and exits with status 0.
