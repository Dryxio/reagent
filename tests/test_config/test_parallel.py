import pytest

from re_agent.config.loader import validate_config
from re_agent.config.schema import ReAgentConfig
from re_agent.core.identity import project_fingerprint


@pytest.mark.parametrize("value", [0, -1, 33, True, 1.5])
def test_parallel_limits(value):
    config = ReAgentConfig()
    config.orchestrator.max_parallel_functions = value
    with pytest.raises(ValueError, match="max_parallel_functions"):
        validate_config(config)


def test_validation_contract_and_identity(tmp_path):
    c = ReAgentConfig()
    c.project_profile.source_root = str(tmp_path)
    original = project_fingerprint(c)
    c.orchestrator.max_parallel_functions = 4
    c.orchestrator.max_parallel_validations = 2
    with pytest.raises(ValueError, match="parallel_safe"):
        validate_config(c)
    c.validation.parallel_safe = True
    c.validation.copy_project = True
    validate_config(c)
    c.validation.copy_project = False
    assert project_fingerprint(c) == original
