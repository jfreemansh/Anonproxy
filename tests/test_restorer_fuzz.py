"""Property-based round-trip guarantees (hypothesis).

The core product promise is that restoration survives whatever a model does
to a surrogate. Hand-written fixtures cover known patterns; these properties
throw randomized combinations at TolerantRestorer/StreamRestorer to catch
classes of breakage nobody wrote down.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from anonproxy.restorer import StreamRestorer, TolerantRestorer

SURROGATE = "host-ab12cd34"
ORIGINAL = "dc01.acme.local"

noise = st.sampled_from(["", "", "", "*", "`", "~", "\u200b", "\u200c"])
style = st.integers(min_value=0, max_value=6)


def mangle(surrogate: str, kind: int) -> str:
    """Deterministically damage a surrogate the way models do."""
    if kind == 0:
        return surrogate
    if kind == 1:
        return surrogate.upper()
    if kind == 2:
        return surrogate.lower()
    if kind == 3:  # emphasis *inside* the token
        return "*".join(surrogate)
    if kind == 4:  # backticks inside
        return "`".join(surrogate)
    if kind == 5:  # unicode hyphen substitution
        return surrogate.replace("-", "\u2011")
    # zero-width sprinkling between every char
    return "\u200b".join(surrogate)


free_text = st.text(
    alphabet=st.characters(blacklist_characters="*`~\u200b\u200c\u2011"),
    max_size=60,
)


@settings(max_examples=50, deadline=None)
@given(prefix=free_text, suffix=free_text, kind=style)
def test_mangled_surrogate_always_restores(prefix: str, suffix: str, kind: int):
    damaged = mangle(SURROGATE, kind)
    assume(SURROGATE not in prefix + suffix)
    assume(damaged not in prefix + suffix)

    text = f"{prefix}{damaged}{suffix}"
    restored = TolerantRestorer().restore(text, [(SURROGATE, ORIGINAL)])

    assert restored == f"{prefix}{ORIGINAL}{suffix}", (
        f"kind={kind} prefix={prefix!r} suffix={suffix!r} -> {restored!r}")


@settings(max_examples=50, deadline=None)
@given(text=free_text)
def test_no_false_positives_without_surrogate(text: str):
    """Benign content must come back byte-identical when nothing matches."""
    assume(SURROGATE not in text)
    assume(mangle(SURROGATE, 3) not in text)
    restored = TolerantRestorer().restore(text, [(SURROGATE, ORIGINAL)])
    assert restored == text


@settings(max_examples=30, deadline=None)
@given(prefix=free_text, suffix=free_text,
       cuts=st.lists(st.integers(min_value=1, max_value=12), min_size=1,
                     max_size=25))
def test_streaming_matches_whole_buffer(prefix: str, suffix: str, cuts):
    """Chunked push() must equal one-shot restore(), for any split points."""
    damaged = mangle(SURROGATE, 4)
    assume(SURROGATE not in prefix + suffix)
    full = f"{prefix}{damaged}{suffix}"

    sr = StreamRestorer([(SURROGATE, ORIGINAL)])
    pos, i, emitted = 0, 0, ""
    while pos < len(full):                 # cycle cut sizes until stream ends
        emitted += sr.push(full[pos:pos + cuts[i % len(cuts)]])
        pos += cuts[i % len(cuts)]
        i += 1
    emitted += sr.flush()

    assert emitted == f"{prefix}{ORIGINAL}{suffix}"
