"""Render one timed-text track for the visible portion of a narrated cut."""
from collections import defaultdict
from html import escape

from .voice import _timestamp


def write_captions(path, cues, duration):
    # mov_text has one active sample at a time. Combine simultaneous speakers,
    # including when a line crosses another speaker's start or the end of the cut.
    end_ms = round(1000 * duration)
    changes = defaultdict(list)
    for ident, (first, last, text) in enumerate(cues):
        first, last = max(0, first), min(end_ms, last)
        if last > first and text.strip():
            changes[first].append((ident, text))
            changes[last].append((ident, None))
    lines, active = [], {}
    previous = 0
    for instant in sorted(changes):
        if active and previous < instant:
            shown = "\n".join(escape(active[i], quote=False) for i in sorted(active))
            lines.append(f"{len(lines)+1}\n{_timestamp(previous)} --> {_timestamp(instant)}\n{shown}\n")
        for ident, text in changes[instant]:
            if text is None:
                active.pop(ident)
            else:
                active[ident] = text
        previous = instant
    if lines:
        path.write_text("\n".join(lines), encoding="utf-8")
    return len(lines)
