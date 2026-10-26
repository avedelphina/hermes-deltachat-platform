import pytest

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
    """Bare .xdc paths: taken only when the file exists on the host (upstream 2.1.0)."""

    def test_extracts_an_existing_absolute_xdc(self, platform_config, tmp_path):
        app = tmp_path / "app.xdc"
        app.write_bytes(b"zip")
        adapter = _make_adapter(platform_config)

        files, remaining = adapter.extract_local_files(
            f"Here is your app at {app} for you."
        )

        assert files == [str(app)]
        assert str(app) not in remaining

    def test_home_relative_path_is_expanded(
        self, platform_config, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "app.xdc").write_bytes(b"zip")
        adapter = _make_adapter(platform_config)

        files, _ = adapter.extract_local_files("Built it: ~/app.xdc done.")

        assert files == [str(tmp_path / "app.xdc")]

    @pytest.mark.parametrize(
        "path", ["/home/user/proj/missing.xdc", "/workspace/app.xdc"]
    )
    def test_path_that_is_not_on_the_host_stays_text(self, platform_config, path):
        """A path only mentioned in prose used to be cut out and pushed as an
        attachment; a bare /workspace/ one was re-sent on every later mention.
        The agent sends sandbox files with a MEDIA: tag."""
        adapter = _make_adapter(platform_config)

        files, remaining = adapter.extract_local_files(f"See {path} for details.")

        assert files == []
        assert path in remaining

    def test_only_the_extracted_occurrence_is_removed(
        self, platform_config, tmp_path, monkeypatch
    ):
        """str.replace() also stripped the same path inside a code sample."""
        import adapter as adapter_mod

        app = tmp_path / "app.xdc"
        app.write_bytes(b"zip")
        text = f"Sent {app}.\n```\ncp build {app}\n```"
        # stand-in for Hermes' masker: blank out the fenced block
        start = text.index("```")
        # the base class adapter.py really uses (conftest may be imported twice)
        monkeypatch.setattr(
            adapter_mod.BasePlatformAdapter,
            "_mask_protected_spans",
            staticmethod(lambda t: t[:start] + " " * (len(t) - start)),
            raising=False,
        )
        adapter = _make_adapter(platform_config)

        files, remaining = adapter.extract_local_files(text)

        assert files == [str(app)]
        assert f"cp build {app}" in remaining


class TestDeliverEachFileOnce:
    @pytest.mark.asyncio
    async def test_media_tag_and_bare_mention_send_one_file(
        self, platform_config, tmp_path
    ):
        from unittest.mock import AsyncMock, patch

        app = tmp_path / "app.xdc"
        app.write_bytes(b"zip")
        adapter = _make_adapter(platform_config)
        base = AsyncMock()
        import adapter as adapter_mod

        with patch.object(
            adapter_mod.BasePlatformAdapter,
            "_deliver_media_attachments",
            base,
            create=True,
        ):
            await adapter._deliver_media_attachments(
                "evt",
                [(str(app), False)],
                [str(app), str(tmp_path / "." / "app.xdc")],
                metadata={},
            )
        kwargs = base.await_args.kwargs
        assert kwargs["media_files"] == [(str(app), False)]
        assert kwargs["local_files"] == []
        assert kwargs["metadata"] == {}


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

    def test_omits_session_key_when_base_does_not_support_it(
        self, monkeypatch, platform_config
    ):
        """Regression (upstream PR #6 follow-up): an older Hermes core whose
        filter_media/local_delivery_paths take a single positional argument
        must not be called with session_key= — that crashes the same way
        omitting it crashes on a newer core."""
        from tests.conftest import MockBasePlatformAdapter

        def old_local(file_paths):
            return list(file_paths)

        def old_media(media_files):
            return list(media_files)

        monkeypatch.setattr(
            MockBasePlatformAdapter,
            "filter_local_delivery_paths",
            staticmethod(old_local),
        )
        monkeypatch.setattr(
            MockBasePlatformAdapter,
            "filter_media_delivery_paths",
            staticmethod(old_media),
        )
        adapter = _make_adapter(platform_config)

        local_result = adapter.filter_local_delivery_paths(
            ["/home/user/report.pdf"], session_key="agent:main:deltachat:dm:12"
        )
        media_result = adapter.filter_media_delivery_paths(
            [("/home/user/app.xdc", False)], session_key="agent:main:deltachat:dm:12"
        )

        assert local_result == ["/home/user/report.pdf"]
        assert media_result == [("/home/user/app.xdc", False)]


class TestNoHardcodedWorkspace:
    """#3: non-Docker deployments have no /workspace and cannot create it."""

    def test_platform_hint_points_at_cwd_not_workspace(self):
        from unittest.mock import MagicMock

        import adapter

        ctx = MagicMock()
        adapter.register_platform(ctx)
        hint = ctx.register_platform.call_args.kwargs["platform_hint"]
        assert "current working directory" in hint
        assert "write output files to /workspace/" not in hint

    def test_webxdc_skill_snippets_are_cwd_relative(self):
        from pathlib import Path

        skill = (
            Path(__file__).parent.parent / "skills/webxdc-converter/SKILL.md"
        ).read_text()
        in_code = False
        for line in skill.splitlines():
            if line.startswith("```"):
                in_code = not in_code
            elif in_code and "MEDIA:" not in line:
                assert "/workspace/" not in line, line


class TestWebxdcSkillRegistration:
    def test_skill_is_registered_with_its_description(self, monkeypatch):
        """Hermes lists plugin skills with the description given here; it does
        not read SKILL.md's frontmatter for them."""
        import sys
        import types
        from unittest.mock import MagicMock

        import adapter

        utils = types.ModuleType("agent.skill_utils")
        utils.parse_frontmatter = lambda text: (
            {"description": "Webxdc for Delta Chat"},
            text,
        )
        monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
        monkeypatch.setitem(sys.modules, "agent.skill_utils", utils)
        ctx = MagicMock()
        adapter.register_platform(ctx)
        _, kwargs = ctx.register_skill.call_args
        assert kwargs["description"] == "Webxdc for Delta Chat"

    def test_platform_hint_uses_the_name_hermes_registers(self):
        from unittest.mock import MagicMock

        import adapter

        ctx = MagicMock()
        adapter.register_platform(ctx)
        hint = ctx.register_platform.call_args.kwargs["platform_hint"]
        assert "skill_view('deltachat-platform:webxdc-converter')" in hint
        assert "plugin:deltachat-platform" not in hint
