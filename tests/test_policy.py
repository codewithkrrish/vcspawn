"""Tests for the pure policy decisions."""

from vcspawn.policy import is_owner, next_channel_number, should_delete


def test_next_number_starts_at_one():
    assert next_channel_number([]) == 1


def test_next_number_increments():
    assert next_channel_number([1, 2]) == 3


def test_next_number_reuses_gaps():
    assert next_channel_number([1, 3]) == 2
    assert next_channel_number([2, 3]) == 1


def test_next_number_ignores_zero_and_negatives():
    assert next_channel_number([0, -1, 1]) == 2


def test_should_delete_only_when_empty():
    assert should_delete(0) is True
    assert should_delete(1) is False
    assert should_delete(25) is False


def test_is_owner():
    assert is_owner(777, 777) is True
    assert is_owner(777, 888) is False
    assert is_owner(None, 777) is False  # ownerless: nobody owns it
