"""Backend factory — creates a backend from configuration."""

from __future__ import annotations

from re_agent.backend.protocol import REBackend
from re_agent.config.schema import BackendConfig


def create_backend(config: BackendConfig) -> REBackend:
    """Create an RE backend based on config.backend.type.

    Supported types:
        - ``"ghidra-bridge"`` (default): Shells out to a Ghidra CLI tool.
        - ``"stub"``: In-memory stub returning canned data (for testing).

    Raises:
        ValueError: If the backend type is not recognised.
    """
    backend_type = config.type.lower().replace("_", "-")

    if backend_type in ("ghidra-bridge", "ghidra"):
        from re_agent.backend.ghidra_bridge import GhidraBridgeBackend

        return GhidraBridgeBackend(
            cli_path=config.cli_path,
            timeout_s=config.timeout_s,
        )

    if backend_type == "ghidra-json":
        from re_agent.backend.exports import GhidraExportsBackend

        if not config.export_dir:
            raise ValueError("backend.export_dir is required for ghidra-json")
        return GhidraExportsBackend(config.export_dir, config.address_map)

    if backend_type == "stub":
        from re_agent.backend.stub import StubBackend

        return StubBackend()

    raise ValueError(f"Unknown backend type: {config.type!r}. Supported: ghidra-bridge, stub")
