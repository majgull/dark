# probes: what the providers actually do

`thinking_probe.py` sends one fixed prompt to an OpenAI-compatible chat
completions endpoint and prints one JSON line per call, so a claim about a
model's thinking rests on a measurement rather than the provider's
documentation.

    python3 thinking_probe.py <model>[,<model>...] <api>

`<api>` is `reasoning_effort`, which sends the level in a `reasoning_effort`
field, or any other value, which sends it in `chat_template_kwargs.enable_thinking`.
For each model the script tries the levels none, low and medium, each
non-streaming and then streaming.

The endpoint is the `GATE` constant in the script; point it at the endpoint
being probed. Each line carries the model, the level, `usage` as returned,
`reasoning_chars` (length of the `reasoning` or `reasoning_content` field,
summed over a stream) and `content_chars` (length of the answer). Read
`reasoning_chars` before trusting a level's name: a provider may accept a
level and still return no reasoning.
