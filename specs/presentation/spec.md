# Spec: the presentation cut

This is the spec for one feature. Read `AGENTS.md`, `memory/constitution.md` and `specs/user-arm/spec.md` first. `dark demo` already cuts a user-arm run into a captioned video for the operator who checks the run (the **verification cut**). This feature adds a second output from the same records, the **presentation cut**, for a viewer who has never seen the product. Every rule below is enforced by code or a test in this repository; none of it may depend on what the person or agent running `dark demo` happens to know.

## Terms

- **records**: one run's directory as the user arm writes it: `task.json`, `steps.jsonl`, `trace.zip`, `steps/NN.png`, `steps/NN.trail.jsonl`.
- **explainer**: one short video that says, in plain words, how a run is tested: who writes the steps, who acts, who judges, what the colours mean.
- **expected mark**: the place on the page where the judge said the result should appear, drawn before the run's action.
- **actual mark**: the element the tester acted on, in a second colour.

## Requirements

1. **Explainer separate.** The explainer is its own file, built once per dark version by `dark demo --explainer`, and never re-narrated inside a presentation cut. `dark demo --present <records> [--with-explainer]` prepends it only when asked. Test: two presentation cuts share no explainer frames unless the flag is given.
2. **Length follows content.** There is no fixed length cap. Each step gets the time its parts need: the task in its exact words, the expected mark, the actual action and the held result, the verdict and who gave it. A run with more or harder steps makes a longer cut. Test: cut length grows with the number of steps; no constant caps it.
3. **Two colours, both shown.** The expected mark and the actual mark are both drawn, in two fixed colours named in the explainer; when they differ, both stay on screen for the held frame. Test: a step whose click missed the expected target renders both marks.
4. **Plain words.** Caption and voice text pass a check against `docs/concepts.md`: no in-house term appears unless the explainer defines it. Test: a caption containing an undefined term fails the cut.
5. **From the records only.** The cut is generated from the records with no per-demo hand-written configuration and no knowledge outside the records. Test: the cut of a fixture run is byte-stable across two invocations with the same versions.

## Candidate engine

playwright-recast (MIT), which reads `trace.zip` directly: survey in rave `reports/1065-demo-tools.md`, trial in rave `reports/1065-recast-trial.md`. The decision to adopt it is recorded here when the trial is read.
