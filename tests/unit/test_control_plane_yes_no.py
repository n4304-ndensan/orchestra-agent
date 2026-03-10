from __future__ import annotations

import pytest

from orchestra_agent.control_plane import _as_yes_no_bool


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        ("yes", True),
        ("YES", True),
        ("y", True),
        ("true", True),
        ("1", True),
        ("no", False),
        ("NO", False),
        ("n", False),
        ("false", False),
        ("0", False),
    ],
)
def test_as_yes_no_bool_accepts_bool_and_yes_no_strings(raw: object, expected: bool) -> None:
    value = _as_yes_no_bool(raw, field_name="approve", default=False)
    assert value is expected


def test_as_yes_no_bool_uses_default_for_none() -> None:
    assert _as_yes_no_bool(None, field_name="approve", default=True) is True
    assert _as_yes_no_bool(None, field_name="approve", default=False) is False


def test_as_yes_no_bool_rejects_invalid_value() -> None:
    with pytest.raises(ValueError, match="must be a boolean or yes/no string"):
        _as_yes_no_bool("maybe", field_name="approve", default=False)
