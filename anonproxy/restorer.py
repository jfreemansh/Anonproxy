"""
Tolerant + streaming-safe restoration — the core reliability fix.

The original approach restored with a plain ``str.replace(surrogate, original)``.
That breaks the moment the model touches the surrogate:

    surrogate emitted to model:   host-ab12cd9
    model writes back:            **host-ab12cd9**     (bold)
                                  `host-ab12cd9`        (inline code)
                                  HOST-AB12CD9          (case changed)
                                  host‑ab12cd9          (line-wrapped / nbsp)

None of those match an exact substring, so the real value is never restored —
the single biggest source of the ~75% round-trip rate.

`TolerantRestorer` matches a surrogate even when:

* it is wrapped or interrupted by markdown noise (``*  `  ~``) or zero-width chars,
* its case was changed,
* internal whitespace was re-wrapped (matters for multi-word PERSON/ORG values).

It does this by matching against a *normalized projection* of the text while
keeping an index map back to the original, so replacements land on real spans
(and swallow the surrounding ``**`` / backticks). `StreamRestorer` adds a
hold-back buffer so a surrogate split across SSE chunks is still restored whole.
"""
from __future__ import annotations

# characters models inject around/inside tokens that are NEVER part of a surrogate
_NOISE = set("*`~")
_ZEROWIDTH = set("​‌‍⁠﻿")
# wrapper chars to swallow when they hug a matched span
_WRAP = set("*`~_")
# non-breaking / unicode hyphens models sometimes substitute for "-"
_HYPHENS = {"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-"}


def _normalize_char(ch: str) -> str:
    if ch in _HYPHENS:
        return "-"
    if ch.isascii():
        return ch.lower()
    return ch


class TolerantRestorer:
    def __init__(self, tolerant: bool = True):
        self.tolerant = tolerant

    # -- normalized projection + index map ----------------------------------
    def _project(self, text: str):
        """Return (projected_lowercased_text, idx_map).

        ``idx_map[i]`` is the index in ``text`` of projected char ``i``.
        Noise/zero-width chars are dropped; whitespace runs collapse to one space.
        """
        proj: list[str] = []
        idx: list[int] = []
        prev_space = False
        for i, ch in enumerate(text):
            if ch in _NOISE or ch in _ZEROWIDTH:
                continue
            if ch.isspace():
                if prev_space:
                    continue
                proj.append(" ")
                idx.append(i)
                prev_space = True
                continue
            proj.append(_normalize_char(ch))
            idx.append(i)
            prev_space = False
        return "".join(proj), idx

    @staticmethod
    def _project_surrogate(surrogate: str) -> str:
        out: list[str] = []
        prev_space = False
        for ch in surrogate:
            if ch.isspace():
                if not prev_space:
                    out.append(" ")
                prev_space = True
            else:
                out.append(_normalize_char(ch))
                prev_space = False
        return "".join(out)

    def find_spans(self, text: str, mappings: list[tuple[str, str]]):
        """Return non-overlapping ``(start, end, original)`` spans in ``text``.

        ``mappings`` must be ``(surrogate, original)`` ordered longest-surrogate
        first so larger tokens claim their span before nested shorter ones.
        """
        projected, idx = self._project(text)
        consumed = bytearray(len(text))
        spans: list[tuple[int, int, str]] = []

        for surrogate, original in mappings:
            surf = self._project_surrogate(surrogate)
            if not surf:
                continue
            start = 0
            while True:
                p = projected.find(surf, start)
                if p < 0:
                    break
                a, b = p, p + len(surf)
                os, oe = idx[a], idx[b - 1] + 1
                # swallow hugging wrapper characters so no dangling ** / ` remain
                while os > 0 and text[os - 1] in _WRAP:
                    os -= 1
                while oe < len(text) and text[oe] in _WRAP:
                    oe += 1
                if any(consumed[os:oe]):
                    start = b
                    continue
                for i in range(os, oe):
                    consumed[i] = 1
                spans.append((os, oe, original))
                start = b

        spans.sort()
        return spans

    def restore(self, text: str, mappings: list[tuple[str, str]]) -> str:
        if not text or not mappings:
            return text
        if not self.tolerant:
            result = text
            for surrogate, original in mappings:
                result = result.replace(surrogate, original)
            return result

        spans = self.find_spans(text, mappings)
        if not spans:
            return text
        out: list[str] = []
        cursor = 0
        for start, end, original in spans:
            out.append(text[cursor:start])
            out.append(original)
            cursor = end
        out.append(text[cursor:])
        return "".join(out)

    def safe_cut(self, buffer: str, mappings: list[tuple[str, str]], target: int) -> int:
        """Largest cut index <= ``target`` that does not fall *inside* a match.

        Used by the streaming restorer so we never emit half a surrogate.
        """
        if target >= len(buffer):
            target = len(buffer)
        spans = self.find_spans(buffer, mappings)
        cut = target
        for start, end, _ in spans:
            if start < cut < end:
                cut = start
        return max(cut, 0)


class StreamRestorer:
    """Restore surrogates across a stream of text deltas.

    Holds back the tail of the buffer (longer than any surrogate's worst-case
    footprint) so a surrogate split across two SSE chunks is reassembled before
    restoration.  Call :meth:`push` per delta and :meth:`flush` at end.
    """

    def __init__(self, mappings: list[tuple[str, str]], tolerant: bool = True):
        self.restorer = TolerantRestorer(tolerant=tolerant)
        self.mappings = mappings
        self._buf = ""
        # worst-case footprint = surrogate length + a little room for injected noise
        max_surr = max((len(s) for s, _ in mappings), default=0)
        self._hold = max_surr + 16

    def push(self, delta: str) -> str:
        self._buf += delta
        target = len(self._buf) - self._hold
        if target <= 0:
            return ""
        cut = self.restorer.safe_cut(self._buf, self.mappings, target)
        if cut <= 0:
            return ""
        emit = self.restorer.restore(self._buf[:cut], self.mappings)
        self._buf = self._buf[cut:]
        return emit

    def flush(self) -> str:
        emit = self.restorer.restore(self._buf, self.mappings)
        self._buf = ""
        return emit
