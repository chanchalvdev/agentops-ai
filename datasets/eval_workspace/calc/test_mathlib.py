from mathlib import add, seconds_to_minutes


def test_add():
    assert add(2, 3) == 5


def test_seconds_to_minutes():
    assert seconds_to_minutes(300) == 5
