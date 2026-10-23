"""Pure reconciliation ordering, not Docker containment evidence."""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from workers.research.containment import AttemptSandboxStopper


@pytest.mark.parametrize("root", [False, True])
@pytest.mark.parametrize("native", [True, False, None, RuntimeError("synthetic")])
@pytest.mark.parametrize("portable", [True, False, None, RuntimeError("synthetic")])
def test_every_owned_family_checked_even_after_unknown_stop(root, native, portable):
    def fake(value):
        return (
            Mock(side_effect=value)
            if isinstance(value, Exception)
            else Mock(return_value=value)
        )

    old = SimpleNamespace(stop=fake(native))
    dual = SimpleNamespace(stop=fake(portable), stop_attempt=fake(portable))
    attempt = str(uuid4())
    identifier = attempt if root else str(uuid4())
    stopper = AttemptSandboxStopper(attempt, old, dual)
    assert stopper.stop(identifier) is (native is True and portable is True)
    old.stop.assert_called_once_with(identifier)
    (dual.stop_attempt if root else dual.stop).assert_called_once_with(identifier)
    (dual.stop if root else dual.stop_attempt).assert_not_called()


def test_invalid_identifiers_never_reach_any_runner():
    old = SimpleNamespace(stop=Mock())
    dual = SimpleNamespace(stop=Mock(), stop_attempt=Mock())
    stopper = AttemptSandboxStopper(uuid4(), old, dual)
    with pytest.raises(ValueError):
        stopper.stop("unrelated-container-name")
    old.stop.assert_not_called()
    dual.stop.assert_not_called()
    dual.stop_attempt.assert_not_called()
