"""Unit tests for harness adapter dispatch.

We don't run the harness here (that would require an LLM API key + network).
We only test that the dispatch / construction code paths work.
"""

from __future__ import annotations

import pytest

from agentweave_runner.harness import (
    HARNESS_REGISTRY,
    LangGraphAdapter,
    OpenClawAdapter,
    get_harness,
)


@pytest.mark.unit
def test_registry():
    assert set(HARNESS_REGISTRY) == {"langgraph", "openclaw"}


@pytest.mark.unit
def test_get_harness_case_insensitive():
    h = get_harness("LangGraph")
    assert h.name == "langgraph"


@pytest.mark.unit
def test_openclaw_stub_raises():
    h = OpenClawAdapter()
    with pytest.raises(NotImplementedError):
        h.run_task({"id": "t"}, library=None, seed=0)


@pytest.mark.unit
def test_langgraph_adapter_init():
    # The adapter should construct cleanly even if langgraph_harness isn't
    # importable yet; the import is deferred to run_task.
    h = LangGraphAdapter()
    assert h.name == "langgraph"
    assert h.model
