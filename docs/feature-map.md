# Response recovery verification

T-260926-017, Athena, 2026-09-26 KST.

- Prerequisites: Python 3, temporary state/SQLite files and mocked local model and Telegram transports.
- Entry: `python3 -m unittest discover -s tests -q`.
- Steps: submit one question with malformed model content, flush its failure response, submit another with a valid answer, then reopen the bridge using the same temporary state.
- Expected: the failed question is absent from conversation history; the next answer is saved and sent through the mock transport; restart has no pending question to replay. Non-object JSON is reported as a safe connection error without including response bodies or URLs.
- Evidence: `tests/test_response_recovery.py` covers response shapes and `test_malformed_answer_does_not_replay_or_block_next_request` in `tests/test_runtime_regressions.py` covers queue/history recovery. Normal provider paths remain covered by existing tests.
- Boundary: no real model inference, Telegram send or production state modification. Actual network delivery and process restart require approved rollout; these tests do not guarantee exactly-once delivery across ambiguous network failures.
