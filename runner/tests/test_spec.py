import unittest

from dark import spec


class Classes(unittest.TestCase):
    def test_partition(self):
        self.assertEqual(set(spec.EXEC_CLASSES) | set(spec.CALL_CLASSES), set(spec.CLASSES))
        self.assertFalse(set(spec.EXEC_CLASSES) & set(spec.CALL_CLASSES))
        self.assertEqual(len(spec.CLASSES), 5)


class States(unittest.TestCase):
    def test_outcomes_are_terminal_states(self):
        for o in spec.OUTCOMES:
            self.assertIn(o, spec.STATES)
            self.assertIn(o, spec.TERMINAL)
        self.assertIn("refused", spec.TERMINAL)

    def test_every_transition_names_known_states(self):
        for frm, to, trigger, who in spec.TRANSITIONS:
            self.assertTrue(frm == "*" or frm in spec.STATES, frm)
            self.assertIn(to, spec.STATES)
            self.assertTrue(trigger)
            self.assertEqual(who, "runner")  # the model never decides a transition

    def test_every_outcome_reachable(self):
        targets = {t for _, t, _, _ in spec.TRANSITIONS}
        for o in spec.OUTCOMES:
            self.assertIn(o, targets, o)

    def test_terminal_states_have_no_exit(self):
        for frm, _, _, _ in spec.TRANSITIONS:
            self.assertNotIn(frm, spec.TERMINAL)
        for t in spec.TERMINAL:
            self.assertFalse(spec.transition_ok(t, "abort"))

    def test_transition_ok(self):
        self.assertTrue(spec.transition_ok("queued", "preflight"))
        self.assertTrue(spec.transition_ok("staging", "pass"))
        self.assertTrue(spec.transition_ok("executing", "abort"))
        self.assertFalse(spec.transition_ok("queued", "pass"))
        self.assertFalse(spec.transition_ok("executing", "pass"))  # only via staging
        self.assertFalse(spec.transition_ok("pass", "executing"))

    def test_structural_never_escalates(self):
        self.assertEqual(spec.ESCALATES, {"fail:capability", "fail:budget"})
        self.assertNotIn("fail:structural", spec.ESCALATES)


class Events(unittest.TestCase):
    def test_registry_shape(self):
        for kind, (req, opt) in spec.EVENTS.items():
            self.assertIsInstance(kind, str)
            self.assertFalse(set(req) & set(opt), kind)
            self.assertFalse(set(req) & spec.BASE_FIELDS, kind)
            self.assertFalse(set(opt) & spec.BASE_FIELDS, kind)

    def test_event_ok(self):
        ok, why = spec.event_ok("park", {"task": "t", "reason": "window"})
        self.assertTrue(ok, why)
        ok, why = spec.event_ok("park", {"task": "t"})
        self.assertFalse(ok)
        self.assertIn("missing", why)
        ok, why = spec.event_ok("park", {"task": "t", "reason": "x", "extra": 1})
        self.assertFalse(ok)
        self.assertIn("undeclared", why)
        ok, why = spec.event_ok("nope", {})
        self.assertFalse(ok)
        self.assertIn("unregistered", why)

    def test_base_fields_tolerated(self):
        ok, _ = spec.event_ok("abort", {"task": "t", "run": "r", "ts": 1, "iso": "x", "v": 2, "kind": "abort"})
        self.assertTrue(ok)

    def test_run_end_carries_the_cost_columns(self):
        req = set(spec.EVENTS["run.end"][0])
        for col in ("tier", "outcome", "seconds", "tokens_in", "tokens_out", "paid", "watts_class"):
            self.assertIn(col, req)


class Tags(unittest.TestCase):
    def test_fail_kinds_map_to_outcomes(self):
        for kind, outcome in spec.FAIL_KIND_OUTCOME.items():
            self.assertIn(outcome, spec.OUTCOMES, kind)
        self.assertNotIn("pass", spec.FAIL_KIND_OUTCOME.values())

    def test_tag_ok(self):
        self.assertTrue(spec.tag_ok("done", {"outcome": "ok", "calls": 1, "tokens_in": 1,
                                             "tokens_out": 1, "reasoning_chars": 0, "seconds": 3}))
        self.assertFalse(spec.tag_ok("done", {"outcome": "ok"}))
        self.assertFalse(spec.tag_ok("nope", {}))
        self.assertTrue(spec.tag_ok("beat", {"at": 1, "calls": 0}))


class StructuralReasons(unittest.TestCase):
    """The widened structural set: a failure before the model's first call
    or after its last push is not the model's doing, and says why."""

    def test_every_structural_kind_names_a_reason(self):
        for kind, outcome in spec.FAIL_KIND_OUTCOME.items():
            if outcome == "fail:structural":
                self.assertIn(kind, spec.STRUCTURAL_REASONS, kind)
                self.assertTrue(spec.structural_reason(kind))

    def test_no_reason_for_a_failure_that_is_the_models_doing(self):
        for kind in ("calls", "no_blocks", "reasoning", "seconds", "abort"):
            self.assertIsNone(spec.structural_reason(kind))

    def test_the_table_has_no_kind_that_is_not_structural(self):
        for kind in spec.STRUCTURAL_REASONS:
            self.assertEqual(spec.FAIL_KIND_OUTCOME[kind], "fail:structural", kind)

    def test_a_gitea_preparation_failure_can_end_a_run(self):
        self.assertTrue(spec.transition_ok("preflight", "fail:structural"))


if __name__ == "__main__":
    unittest.main()
