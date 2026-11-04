"""Structure-aware text chunking.

Splits text into chunks of at most ``chunk_size`` characters, preferring to cut
at natural boundaries so a chunk reads as a coherent passage:

    paragraph break  >  line break  >  sentence end  >  clause  >  word

Consecutive chunks share a short tail (``overlap`` characters, trimmed to a word
boundary) so an idea that straddles a cut is still retrievable from either side.
If a piece has none of those boundaries (e.g. one very long token) it is cut at
exactly ``chunk_size`` characters, without overlap.

Real systems often use a token-aware splitter (e.g. LangChain's
RecursiveCharacterTextSplitter); this keeps the dependency surface small and the
behaviour deterministic and easy to test.
"""

from __future__ import annotations

_SEPARATORS = ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ")


def _split(text: str, separators: tuple[str, ...], size: int) -> list[str]:
    """Break ``text`` into pieces no longer than ``size``, keeping separators."""
    if len(text) <= size:
        return [text]
    if not separators:
        return [text[i : i + size] for i in range(0, len(text), size)]

    sep, rest = separators[0], separators[1:]
    parts = text.split(sep)
    if len(parts) == 1:
        return _split(text, rest, size)

    pieces: list[str] = []
    last = len(parts) - 1
    for i, part in enumerate(parts):
        piece = part + (sep if i < last else "")
        if len(piece) <= size:
            pieces.append(piece)
        else:
            pieces.extend(_split(piece, rest, size))
    return pieces


def _overlap_tail(chunk: str, overlap: int) -> str:
    if overlap <= 0:
        return ""
    tail = chunk[-overlap:]
    # Start on a word boundary so the overlap does not begin mid-word.
    space = tail.find(" ")
    if space != -1 and space < len(tail) - 1:
        tail = tail[space + 1 :]
    return tail


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 100) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    current = ""
    for piece in _split(text, _SEPARATORS, chunk_size):
        if current and len(current) + len(piece) > chunk_size:
            chunks.append(current.strip())
            candidate = _overlap_tail(current, overlap) + piece
            current = candidate if len(candidate) <= chunk_size else piece
        else:
            current += piece
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c]
