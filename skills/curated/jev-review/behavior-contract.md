# Portable JEV CLI behavior contract

Target: Python scripts/jev.py in the packaged jev-review directory. Source-blind validator may read this contract, SKILL.md, USAGE.md, examples and CLI output, but no script or tests.

1. --self-test passes offline with no API credential.
2. --dry-run validates the included example offline; result says validated, advisory_only true, review_required true and has source/request SHA256 strings.
3. Valid input with no credential returns unavailable and exit3, with no traceback or claim that JEV ran.
4. Malformed JSON, unsupported question types, empty criteria and oversized input return invalid and exit2, with no traceback.
5. Identical requests preserve both hashes; changing source changes both; changing only a question preserves source hash and changes request hash.
6. Copying the folder to a different directory preserves offline behavior and example usability; no host-specific paths or imports required.
7. A live call with an authorized provider key returns validated enums and finite confidence0..1, preserves both advisory flags, and emits no credentials or raw source. If validator has no approved credential access, mark this clause blocked; parent supplies separate live proof.
8. A provider failure remains unavailable; no automatic downstream actions occur. Network failure can be source-blind tested with a dead HTTPS proxy and a dummy key, without submitting to provider.
