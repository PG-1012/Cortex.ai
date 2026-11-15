import pytest

from cortexhub.vault import Vault


@pytest.fixture
def vault(tmp_path):
    v = Vault(tmp_path / "CortexVault")
    v.init()
    return v


@pytest.fixture
def server_env(tmp_path, monkeypatch):
    """Point the MCP server at a fresh temp vault."""
    from cortexhub import server

    monkeypatch.setenv("CORTEX_VAULT_PATH", str(tmp_path / "ServerVault"))
    server.vault.cache_clear()
    yield tmp_path / "ServerVault"
    server.vault.cache_clear()


ROBOTICS_BODY = """# Robotics Kinematics 6-Week Plan

## Week 1: Spatial Descriptions
Rotation matrices, frames.

## Week 2: Forward Kinematics
DH parameters.

## Open Questions
- Solver choice
"""
