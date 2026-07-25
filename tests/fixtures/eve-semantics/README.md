# Synthetic Eve semantic fixtures

These fixtures are constructed test data. They were written to exercise the
documented Eve, OpenInference, Vercel AI SDK, and provider-compatible attribute
shapes without retaining model input/output, tool arguments/results, endpoints,
credentials, bearer tokens, or host paths.

`synthetic-public-aggregate.jsonl` is deliberately calibrated to the accepted
public FailureReport aggregate of 498161 input tokens and 22392 output tokens.
It proves Shea Halo's normalization, provenance, topology, and arithmetic
behavior against those published numbers. It was not copied, exported, or
derived from the original trace, and it is not evidence that Shea Halo parsed
that unavailable source.

The remaining files isolate identical aliases, conflicting aliases, absent
optional model metadata, ambiguous agent attribution, and an enclosing AGENT
token aggregate that could otherwise be double-counted with its child LLM.
