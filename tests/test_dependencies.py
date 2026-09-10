from re_agent.orchestrator.dependencies import prerequisites


def test_diamond_and_external_dependencies():
    result = prerequisites(["a", "b", "c", "d"], {
        "a": {"b", "c"}, "b": {"d"}, "c": {"d", "external"}})
    assert result == {"a": {"b", "c"}, "b": {"d"}, "c": {"d"}, "d": set()}


def test_cycle_has_deterministic_serial_order():
    result = prerequisites(["a", "b", "c"], {"a": {"b"}, "b": {"a", "c"}})
    assert result == {"a": {"c"}, "b": {"a"}, "c": set()}


def test_deep_graph_does_not_use_python_recursion():
    order = [str(n) for n in range(5000)]
    result = prerequisites(order, {str(n): {str(n+1)} for n in range(4999)})
    assert result["0"] == {"1"}
    assert result["4999"] == set()
