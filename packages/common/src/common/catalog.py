"""Names shared by several services.

``KNOWN_TOOL_NAMES`` is the list of tools an agent may be created with. The
gateway validates requests against it, and agent-runtime checks at import time
that its tool registry implements exactly these names, so the two cannot drift.
"""

from __future__ import annotations

KNOWN_TOOL_NAMES: frozenset[str] = frozenset({"calculator", "word_count"})
