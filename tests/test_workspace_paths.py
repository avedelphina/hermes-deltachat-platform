"""Tests for workspace/agent path handling in the Delta Chat adapter.

Covers the container->host mapping (with traversal containment) and the
generalized bare-.xdc / MEDIA .xdc extractors that support both the Docker
sandbox (/workspace/) and non-Docker deployments (agent's real cwd). All
cases exercise pure/near-pure adapter methods and need no live RPC.
"""

# conftest.py installs the gateway mocks, so importing adapter here is safe.
from adapter import DeltaChatAdapter


def _make_adapter(platform_config):
    """Construct an adapter without touching RPC (mirrors integration tests)."""
    return DeltaChatAdapter(platform_config)


class TestContainerWorkspaceToHost:
    """_container_workspace_to_host mapping + traversal containment."""

    def test_maps_workspace_path_to_host_sandbox(self, monkeypatch, tmp_path):
        # Make the fallback (get_hermes_home) deterministic. tools.environments
        # is not importable in the test env, so the ImportError branch is used.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        host = DeltaChatAdapter._container_workspace_to_host("/workspace/app.xdc")

        assert host is not None
        # The resolved host path lives under the sandbox workspace root.
        assert host.endswith("docker/default/workspace/app.xdc")
        assert "sandboxes" in host

    def test_traversal_escape_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        assert (
            DeltaChatAdapter._container_workspace_to_host(
                "/workspace/../../../etc/passwd"
            )
            is None
        )
        # A .pdf escape is rejected the same way.
        assert (
            DeltaChatAdapter._container_workspace_to_host(
                "/workspace/../../secret/report.pdf"
            )
            is None
        )

    def test_non_workspace_path_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        assert (
            DeltaChatAdapter._container_workspace_to_host("/home/user/app.xdc") is None
        )


class TestExtractLocalFiles:
    """Generalized bare-.xdc extractor: Docker /workspace/ and non-Docker cwd paths."""

    def test_extracts_bare_absolute_non_workspace_xdc(self, platform_config):
        adapter = _make_adapter(platform_config)
        content = "Here is your app at /home/user/proj/app.xdc for you."

        files, _remaining = adapter.extract_local_files(content)

        assert "/home/user/proj/app.xdc" in files

    def test_extracts_bare_home_relative_xdc(self, platform_config):
        adapter = _make_adapter(platform_config)
        content = "Built it: ~/projects/app.xdc done."

        files, _remaining = adapter.extract_local_files(content)

        assert "~/projects/app.xdc" in files

    def test_still_extracts_workspace_xdc(self, platform_config):
        """Regression: Docker /workspace/ paths must still be picked up."""
        adapter = _make_adapter(platform_config)
        content = "Built it: /workspace/app.xdc done."

        files, _remaining = adapter.extract_local_files(content)

        assert "/workspace/app.xdc" in files


class TestExtractMedia:
    """General MEDIA .xdc extractor regression guard."""

    def test_extracts_media_absolute_xdc(self, platform_config):
        adapter = _make_adapter(platform_config)

        media, _remaining = adapter.extract_media("MEDIA:/home/user/app.xdc")

        assert any(p == "/home/user/app.xdc" for p, _ in media)

    def test_extracts_media_home_xdc(self, platform_config):
        adapter = _make_adapter(platform_config)

        media, _remaining = adapter.extract_media("MEDIA:~/app.xdc")

        assert any(p == "~/app.xdc" for p, _ in media)


class TestFilterLocalDeliveryPaths:
    """filter_local_delivery_paths: workspace remap vs. non-Docker passthrough."""

    def test_non_workspace_path_passed_through_to_base_validator(self, platform_config):
        adapter = _make_adapter(platform_config)

        result = adapter.filter_local_delivery_paths(["/home/user/report.pdf"])

        # No /workspace/ prefix -> not remapped, flows to the base (mocked
        # passthrough) validator unchanged.
        assert result == ["/home/user/report.pdf"]

    def test_workspace_path_remapped_via_cache_copy(self, monkeypatch, platform_config):
        adapter = _make_adapter(platform_config)
        monkeypatch.setattr(
            adapter,
            "_copy_container_file_to_cache",
            lambda p: "/cache/documents/app.xdc",
        )

        result = adapter.filter_local_delivery_paths(["/workspace/app.xdc"])

        assert result == ["/cache/documents/app.xdc"]

    def test_workspace_path_dropped_when_cache_copy_fails(
        self, monkeypatch, platform_config
    ):
        adapter = _make_adapter(platform_config)
        monkeypatch.setattr(adapter, "_copy_container_file_to_cache", lambda p: None)

        result = adapter.filter_local_delivery_paths(["/workspace/missing.xdc"])

        assert result == []

    def test_accepts_session_key_kwarg(self, platform_config):
        """Regression: Hermes core calls these as
        self.filter_media_delivery_paths(media_files, session_key=...) /
        self.filter_local_delivery_paths(file_paths, session_key=...).
        Dropping the extra kwarg silently (no session_key param, no
        **kwargs) breaks every reply with a MEDIA directive at runtime."""
        adapter = _make_adapter(platform_config)

        local_result = adapter.filter_local_delivery_paths(
            ["/home/user/report.pdf"], session_key="agent:main:deltachat:dm:12"
        )
        media_result = adapter.filter_media_delivery_paths(
            [("/home/user/app.xdc", False)], session_key="agent:main:deltachat:dm:12"
        )

        assert local_result == ["/home/user/report.pdf"]
        assert media_result == [("/home/user/app.xdc", False)]
