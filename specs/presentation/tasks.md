# Tasks: the presentation cut

- [ ] 1. `dark demo --explainer` builds the explainer once per version; test for requirement 1.
- [ ] 2. `--present`: per-step timing from content, no cap; test for requirement 2.
- [ ] 3. Expected and actual marks in two colours from `steps.jsonl`; test for requirement 3.
  - Recording done: the user arm's judge writes `expected` and `expected_why` on each step's line in `steps.jsonl` (`runner/tests/test_user.py` `Judge`). Drawing both marks in `dark demo` is not done.
- [ ] 4. Plain-words check against `docs/concepts.md`; test for requirement 4.
- [ ] 5. Layout: transcript beside, step strip above, no subtitle stream; test for requirement 6.
- [ ] 6. Narration check from rave `rave-narrate`, aligned timing; test for requirement 7.
- [ ] 7. Adopt or reject playwright-recast after rave `reports/1065-recast-trial.md`; record the decision in `spec.md`.
