"""Child profile / conversation session state for the main chat graph.

State schemas and helpers for the LangGraph dialogue graph:
- per-turn state (messages, model responses, audit decisions)
- session-level metadata (child_user_id, session_id, created_at)

Step 2 is skeleton only; business logic lives in Step 6.
"""
