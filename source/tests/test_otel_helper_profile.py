# ABOUTME: Tests for otel-helper profile resolution (--profile flag) across
# ABOUTME: the cache path, credential-process subprocess, and collector sidecar.

"""Profile resolution regression tests.

The otel-helper previously resolved its profile ONLY from AWS_PROFILE, so a
Claude Code config pointing the helper at a non-default profile was ignored.
An explicit ``--profile`` must win over the environment and be exported so
every downstream consumer (cache path, credential-process subprocess,
collector sidecar) resolves the same profile.
"""

import os
import sys


def _parse(monkeypatch, argv):
    from otel_helper.__main__ import parse_args

    monkeypatch.setattr(sys, "argv", ["otel-helper"] + argv)
    return parse_args()


class TestProfileResolution:
    def test_profile_flag_overrides_env(self, monkeypatch):
        monkeypatch.setenv("AWS_PROFILE", "EnvProfile")
        _parse(monkeypatch, ["--profile", "FlagProfile"])
        # The flag must be exported so cache path, credential-process, and the
        # collector sidecar all resolve the SAME profile.
        assert os.environ["AWS_PROFILE"] == "FlagProfile"

    def test_no_flag_leaves_env_untouched(self, monkeypatch):
        monkeypatch.setenv("AWS_PROFILE", "EnvProfile")
        _parse(monkeypatch, [])
        assert os.environ["AWS_PROFILE"] == "EnvProfile"

    def test_cache_path_follows_profile_flag(self, monkeypatch, tmp_path):
        from otel_helper.__main__ import get_cache_path

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("AWS_PROFILE", "EnvProfile")
        _parse(monkeypatch, ["--profile", "FlagProfile"])
        assert get_cache_path().name == "FlagProfile-otel-headers.json"
