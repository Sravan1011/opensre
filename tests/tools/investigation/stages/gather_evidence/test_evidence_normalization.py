"""Tool-owned evidence normalization in the generic gather loop (issue #3687).

The investigation gather loop no longer hard-codes vendor-specific output
shaping. It stores raw output plus a ``tool_outputs`` trail and delegates
report-facing key shaping to each tool's ``normalize_evidence`` hook. These
tests pin the generic merge behavior, the Grafana normalizers' parity with the
previously hard-coded keys, and the open/closed invariant that the loop carries
no ``query_grafana`` branches.
"""

from __future__ import annotations

import inspect

from core.tool_framework.registered_tool import REGISTERED_TOOL_ATTR, RegisteredTool
from core.tool_framework.tool_decorator import tool
from tools.investigation.stages.gather_evidence import tools as gather_tools
from tools.investigation.stages.gather_evidence.tools import (
    merge_tool_evidence,
    tool_by_name,
)
from tools.registry import get_registered_tools


def _grafana_tool(name: str) -> RegisteredTool:
    registered = {t.name: t for t in get_registered_tools("investigation")}
    assert name in registered, f"{name} not registered for investigation"
    return registered[name]


def test_merge_stores_raw_output_and_tool_outputs_trail() -> None:
    evidence: dict[str, object] = {}
    output = {"logs": [1, 2]}
    merge_tool_evidence(evidence, "query_grafana_logs", output, {"service_name": "svc"})

    assert evidence["query_grafana_logs"] == output
    assert evidence["tool_outputs"] == [
        {
            "tool_name": "query_grafana_logs",
            "tool_args": {"service_name": "svc"},
            "data": output,
        }
    ]


def test_merge_without_tool_skips_normalization() -> None:
    """No resolved tool (or no hook) => only raw output, no derived keys, no crash."""
    evidence: dict[str, object] = {}
    merge_tool_evidence(evidence, "query_grafana_logs", {"logs": [1]}, {}, tool=None)

    assert evidence["query_grafana_logs"] == {"logs": [1]}
    assert "grafana_logs" not in evidence


def test_merge_non_dict_output_only_records_raw() -> None:
    evidence: dict[str, object] = {}
    merge_tool_evidence(evidence, "some_tool", "not-a-dict", {})
    assert evidence["some_tool"] == "not-a-dict"
    assert "tool_outputs" in evidence


def test_grafana_logs_normalizer_matches_legacy_keys() -> None:
    evidence: dict[str, object] = {}
    output = {
        "logs": [{"line": "a"}],
        "error_logs": [{"line": "e"}],
        "query": '{app="x"}',
        "service_name": "checkout",
    }
    merge_tool_evidence(
        evidence, "query_grafana_logs", output, {}, tool=_grafana_tool("query_grafana_logs")
    )

    assert evidence["grafana_logs"] == [{"line": "a"}]
    assert evidence["grafana_error_logs"] == [{"line": "e"}]
    assert evidence["grafana_logs_query"] == '{app="x"}'
    assert evidence["grafana_logs_service"] == "checkout"


def test_grafana_traces_and_alert_rules_and_service_names() -> None:
    evidence: dict[str, object] = {}
    merge_tool_evidence(
        evidence,
        "query_grafana_traces",
        {"traces": ["t1"], "pipeline_spans": ["s1"]},
        {},
        tool=_grafana_tool("query_grafana_traces"),
    )
    merge_tool_evidence(
        evidence,
        "query_grafana_alert_rules",
        {"rules": ["r1"]},
        {},
        tool=_grafana_tool("query_grafana_alert_rules"),
    )
    merge_tool_evidence(
        evidence,
        "query_grafana_service_names",
        {"service_names": ["svc"]},
        {},
        tool=_grafana_tool("query_grafana_service_names"),
    )

    assert evidence["grafana_traces"] == ["t1"]
    assert evidence["grafana_pipeline_spans"] == ["s1"]
    assert evidence["grafana_alert_rules"] == ["r1"]
    assert evidence["grafana_service_names"] == ["svc"]


def test_grafana_metrics_accumulate_across_calls() -> None:
    """Per-metric results accumulate; the loop shallow-merges dict values."""
    evidence: dict[str, object] = {}
    metrics_tool = _grafana_tool("query_grafana_metrics")

    # metric_name comes from the output on the first call...
    merge_tool_evidence(
        evidence,
        "query_grafana_metrics",
        {"metric_name": "cpu", "metrics": [1]},
        {},
        tool=metrics_tool,
    )
    # ...and from the tool_input on the second (mirrors the legacy fallback).
    merge_tool_evidence(
        evidence,
        "query_grafana_metrics",
        {"metrics": [2]},
        {"metric_name": "mem"},
        tool=metrics_tool,
    )

    assert set(evidence["grafana_metric_results"].keys()) == {"cpu", "mem"}  # type: ignore[union-attr]
    assert evidence["grafana_metrics"] == [2]


def test_grafana_metrics_without_name_skips_per_metric_bucket() -> None:
    evidence: dict[str, object] = {}
    merge_tool_evidence(
        evidence,
        "query_grafana_metrics",
        {"metrics": [1]},
        {},
        tool=_grafana_tool("query_grafana_metrics"),
    )
    assert evidence["grafana_metrics"] == [1]
    assert "grafana_metric_results" not in evidence


def test_all_grafana_tools_declare_normalizer() -> None:
    for name in (
        "query_grafana_logs",
        "query_grafana_metrics",
        "query_grafana_traces",
        "query_grafana_alert_rules",
        "query_grafana_service_names",
    ):
        assert callable(_grafana_tool(name).normalize_evidence), name


def test_tool_by_name_resolves_and_misses() -> None:
    tools = list(get_registered_tools("investigation"))
    assert tool_by_name(tools, "query_grafana_logs") is not None
    assert tool_by_name(tools, "not_a_real_tool") is None


def test_gather_loop_has_no_vendor_specific_branches() -> None:
    """Open/closed invariant: the core gather loop must not name a vendor tool.

    A new vendor is added by declaring ``normalize_evidence`` on its tool, never
    by editing this module. Guard against silently reintroducing the old
    ``if tool_name == "query_grafana_*"`` chain.
    """
    source = inspect.getsource(gather_tools)
    assert "query_grafana" not in source
    assert "grafana_logs" not in source


def test_tool_decorator_threads_normalize_evidence() -> None:
    def _normalizer(output: dict, _tool_input: dict) -> dict:
        return {"derived": output.get("value")}

    @tool(
        name="_normalizer_probe_tool",
        source="knowledge",
        description="probe",
        normalize_evidence=_normalizer,
    )
    def _probe() -> dict:
        return {}

    registered = getattr(_probe, REGISTERED_TOOL_ATTR)
    assert registered.normalize_evidence is _normalizer

    evidence: dict[str, object] = {}
    merge_tool_evidence(evidence, "_normalizer_probe_tool", {"value": 42}, {}, tool=registered)
    assert evidence["derived"] == 42


def test_registered_tool_rejects_non_callable_normalizer() -> None:
    import pytest

    with pytest.raises(TypeError):
        RegisteredTool.from_function(
            lambda: {},
            name="_bad_normalizer_tool",
            source="knowledge",
            normalize_evidence="not-callable",  # type: ignore[arg-type]
        )
