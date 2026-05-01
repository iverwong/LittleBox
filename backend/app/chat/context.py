"""Context aggregation: audit state, rolling summaries, guidance injection.

Collects per-session context from audit records and rolling summaries
to build the `inject_guidance` node's input. Downstream nodes consume
this context to produce safety-aware responses.

Step 2 is skeleton only; business logic lives in Step 4.
"""
