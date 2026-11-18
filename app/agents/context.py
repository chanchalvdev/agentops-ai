"""Bounded model input.

The system prompt and the original question are always kept. Older turns are dropped whole,
oldest first, so an assistant tool request is never separated from its tool results (a dangling
tool call confuses models and some providers reject it).

Dropped turns may hold evidence the answer must still cite (an [S2] shown five turns ago stays
citable). Those labels are not silently lost: a short recap of each one (file + opening text)
replaces them, inside a reserved slice of the budget.
"""

import re
from collections.abc import Callable

from app.llm.schemas import Message

_LABEL = re.compile(r'<(?:source|tool_result) id="([ST]\d+)"')
RECAP_SHARE = 4  # at most 1/4 of the budget goes to recapping dropped evidence


def _groups(messages: list[Message]) -> list[list[Message]]:
    groups: list[list[Message]] = []
    for m in messages:
        if m.role == "tool" and groups:
            groups[-1].append(m)
        else:
            groups.append([m])
    return groups


def _size(group: list[Message]) -> int:
    return sum(len(m.content) + sum(len(str(c.arguments)) for c in m.tool_calls) for m in group)


def bound_history(
    messages: list[Message],
    max_chars: int,
    keep_head: int = 2,
    recap: Callable[[list[str], int], str] | None = None,
) -> list[Message]:
    head, rest = messages[:keep_head], messages[keep_head:]
    budget = max_chars - _size(head)
    groups = _groups(rest)
    if recap is not None and sum(_size(g) for g in groups) > budget:
        budget -= max_chars // RECAP_SHARE  # trimming will happen: reserve room for the recap
    kept: list[list[Message]] = []
    for group in reversed(groups):
        size = _size(group)
        if size > budget and kept:
            break
        kept.append(group)  # the newest group is always kept, even if alone it is over budget
        budget -= size
    dropped = len(groups) - len(kept)
    note = []
    if dropped:
        text = f"[{dropped} earlier turn(s) omitted to fit the context.]"
        labels = [label for g in groups[:dropped] for m in g for label in _LABEL.findall(m.content)]
        if recap is not None and labels:
            text += " Sources shown in those turns remain citable; recap:\n" + recap(
                list(dict.fromkeys(labels)), max_chars // RECAP_SHARE
            )
        note = [Message(role="user", content=text)]
    return [*head, *note, *(m for group in reversed(kept) for m in group)]
