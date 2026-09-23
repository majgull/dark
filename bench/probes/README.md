# probes: what the providers actually do

Raw replies to one fixed prompt, kept so the reports' statements about thinking and tokens rest on a measurement, not on the provider's documentation.

- `thinking-2026-09-03.jsonl`: `thinking_probe.py` against the ollama-cloud endpoint the executor uses (`http://git-host:11434/v1`), both cloud tiers, `reasoning_effort` none / low / medium, non-streaming and streaming. Fields: `usage` as returned, `reasoning_chars` = length of the `reasoning` field, `content_chars` = length of the answer.

Read on 2026-09-03:

- deepseek-v4-flash:cloud: at `none` no `reasoning` field and the answer alone accounts for `completion_tokens`; at `low` and `medium` the reasoning comes back in the reply and `completion_tokens` includes it (558 tokens for 1575 + 305 chars). So the ledger's `reasoning_chars` for this tier is the thinking that happened, and `tokens_out` bills it.
- glm-5.3-flash:cloud: at `none` no `reasoning` field but a long answer (3536 to 5821 chars); at `low` 22 chars of reasoning, effectively none; at `medium` 4848 to 10643 chars. "low" on this tier is not a thinking level in any useful sense; a report must show the measured `reasoning_chars` per run, never the level's name.
- Streaming and non-streaming replies to the same prompt differ in length (sampling); the level's effect is visible in both.
