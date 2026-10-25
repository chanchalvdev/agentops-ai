"""Bounded model input.

The system prompt and the original question are always kept. Older turns are dropped whole,
oldest first, so an assistant tool request is never separated from its tool results (a dangling
tool call confuses models and some providers reject it).
"""

from app.llm.schemas import Message


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


def bound_history(messages: list[Message], max_chars: int, keep_head: int = 2) -> list[Message]:
    head, rest = messages[:keep_head], messages[keep_head:]
    budget = max_chars - _size(head)
    kept: list[list[Message]] = []
    for group in reversed(_groups(rest)):
        size = _size(group)
        if size > budget and kept:
            break
        kept.append(group)  # the newest group is always kept, even if alone it is over budget
        budget -= size
    dropped = len(_groups(rest)) - len(kept)
    note = (
        [Message(role="user", content=f"[{dropped} earlier turn(s) omitted to fit the context.]")]
        if dropped
        else []
    )
    return [*head, *note, *(m for group in reversed(kept) for m in group)]
