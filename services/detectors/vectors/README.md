# Detector framework vectors

`v1.json` pins what FRAMEWORK_VERSION 1.0.0 produces for fixed inputs: detector version strings,
finding ids and canonical finding bytes. `tests/test_vectors.py` recomputes every entry. A
change to any of them is a framework change: bump FRAMEWORK_VERSION, add a `framework.lock.json`
entry and a new vectors file; never edit `v1.json`.
