"""`provider_failure_reason`: the vendor-neutral label filter the runner calls (ADR 0018 amendment
2026-11-18). Only a plain `str` in `PROVIDER_FAILURE_REASONS` on a `ProviderError` passes, as the
set's own string; anything else is "unclassified", and it never raises."""

from __future__ import annotations

from typing import Any

import pytest

from divesafe.models.llm import (
    PROVIDER_FAILURE_REASONS,
    ProviderError,
    provider_failure_reason,
)


class _Tagged(str):
    pass


class _AlwaysEqual:
    def __eq__(self, other: object) -> bool:
        return True

    __hash__ = None  # type: ignore[assignment]


@pytest.mark.parametrize("label", sorted(PROVIDER_FAILURE_REASONS))
def test_every_label_passes_as_the_sets_own_string(label: str) -> None:
    given = "".join(label)  # an equal string, not the set's object
    result = provider_failure_reason(ProviderError("fixed", reason=given))
    assert result == label and type(result) is str
    assert any(result is member for member in PROVIDER_FAILURE_REASONS)


@pytest.mark.parametrize(
    "value",
    ["sk-SECRET", "", _Tagged("read_timeout"), 3, None, b"http_error", ["closed"], _AlwaysEqual()],
    ids=["free_text", "empty", "str_subclass", "int", "none", "bytes", "list", "always_equal"],
)
def test_a_reason_that_is_not_a_plain_label_is_unclassified(value: Any) -> None:
    error = ProviderError("fixed")
    error.reason = value
    assert provider_failure_reason(error) == "unclassified"


def test_an_exception_that_is_not_a_provider_error_is_unclassified() -> None:
    error = RuntimeError("x")
    error.reason = "read_timeout"  # type: ignore[attr-defined]
    assert provider_failure_reason(error) == "unclassified"


def test_a_missing_reason_is_unclassified() -> None:
    class _NoReason(ProviderError):
        def __init__(self) -> None:
            RuntimeError.__init__(self, "fixed")

    assert provider_failure_reason(_NoReason()) == "unclassified"


def test_an_unreadable_instance_dict_is_unclassified_and_does_not_raise() -> None:
    class _NoVars(ProviderError):
        def __getattribute__(self, name: str) -> Any:
            if name == "__dict__":
                raise RuntimeError("SECRET")
            return super().__getattribute__(name)

    assert provider_failure_reason(_NoVars("x", reason="read_timeout")) == "unclassified"


def test_a_reason_property_that_raises_is_unclassified() -> None:
    class _Raising(ProviderError):
        def __init__(self) -> None:
            RuntimeError.__init__(self, "fixed")

        @property  # type: ignore[misc]
        def reason(self) -> str:
            raise RuntimeError("SECRET")

    assert provider_failure_reason(_Raising()) == "unclassified"
