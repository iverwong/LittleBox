"""System prompt builder for the main dialogue.

Skeleton aligned with baseline §7.3:
- single SystemMessage, 5 sections (L1 -> L4 cache-optimized order)
- consumes only age + gender from child_profile
- 8 content slots are stubs; grep `TODO(prompts-content)` to locate.

Actual templates are pending a dedicated review. Business logic
starts at Step 3.
"""
