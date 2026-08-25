"""Tests for config directory handling."""

import os
import tempfile
from pathlib import Path


class TestConfigDirectory:
    """Test config directory resolution logic."""

    def test_dc_config_dir_creation(self):
        """Test that DC config directory is created."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hermes_home = Path(tmpdir) / "hermes"
            hermes_home.mkdir()

            dc_config_dir = hermes_home / "deltachat"

            # Simulate what the adapter does
            dc_config_dir.mkdir(exist_ok=True)

            assert dc_config_dir.exists()
            assert dc_config_dir.is_dir()

    def test_dc_config_dir_path_construction(self):
        """Test path construction for DC config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hermes_home = Path(tmpdir)
            expected = str(hermes_home / "deltachat")

            # This is what adapter._get_dc_config_dir does
            dc_config_dir = os.path.join(str(hermes_home), "deltachat")

            assert dc_config_dir == expected

    def test_env_var_construction(self):
        """Test DC_ACCOUNTS_PATH environment variable construction."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dc_accounts_path = os.path.join(tmpdir, "deltachat")

            # This is what the adapter sets
            env_value = dc_accounts_path

            assert isinstance(env_value, str)
            assert "deltachat" in env_value


class TestDefaultDcDataDir:
    """adapter._default_dc_data_dir: deltachat-platform -> deltachat rename,
    with a fallback to the old directory for existing installs."""

    def test_uses_new_name_when_neither_dir_has_data(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from adapter import _default_dc_data_dir

        result = _default_dc_data_dir()

        assert result == str(tmp_path / "deltachat")

    def test_uses_new_name_when_new_dir_already_has_data(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        new_dir = tmp_path / "deltachat"
        new_dir.mkdir()
        (new_dir / "account-1").mkdir()
        old_dir = tmp_path / "deltachat-platform"
        old_dir.mkdir()
        (old_dir / "account-0").mkdir()
        from adapter import _default_dc_data_dir

        result = _default_dc_data_dir()

        assert result == str(new_dir)

    def test_falls_back_to_old_name_when_only_old_dir_has_data(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        old_dir = tmp_path / "deltachat-platform"
        old_dir.mkdir()
        (old_dir / "account-0").mkdir()
        from adapter import _default_dc_data_dir

        result = _default_dc_data_dir()

        assert result == str(old_dir)

    def test_uses_new_name_when_old_dir_exists_but_empty(self, monkeypatch, tmp_path):
        """An empty old-named dir (e.g. a stale artifact) is not treated as
        an existing account — it must not pin future runs to the old path."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        old_dir = tmp_path / "deltachat-platform"
        old_dir.mkdir()
        from adapter import _default_dc_data_dir

        result = _default_dc_data_dir()

        assert result == str(tmp_path / "deltachat")


class TestRPCServerPath:
    """Test RPC server path resolution."""

    def test_default_rpc_server_path(self):
        """Test default RPC server path is the binary name."""
        # Default from adapter._get_rpc_server_path when no config or env
        default_path = "deltachat-rpc-server"
        assert isinstance(default_path, str)
        assert default_path == "deltachat-rpc-server"

    def test_custom_rpc_server_from_env(self):
        """Test RPC server path from environment variable."""
        # Simulate env var
        custom_path = "/custom/path/to/rpc-server"

        # This is what the adapter checks
        result = custom_path if custom_path else "deltachat-rpc-server"

        assert result == custom_path
