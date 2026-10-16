"""Fast, network-free coverage for instance identity and destructive boundaries."""
import importlib.util
import os
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "simpleaudit_studio" / "lifecycle.py"
spec = importlib.util.spec_from_file_location("local_lifecycle", MODULE)
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SIMPLEAUDIT_DATA_DIR", raising=False)
    monkeypatch.delenv("SIMPLEAUDIT_SQLITE_PATH", raising=False)
    (tmp_path / "home").mkdir()
    return tmp_path


def test_two_worktrees_have_distinct_ids_and_data(environment):
    a = lifecycle.Instance("dev", environment / "main")
    b = lifecycle.Instance("dev", environment / "branch")
    assert a.key != b.key
    assert a.data != b.data
    assert a.db != b.db
    assert a.data.is_relative_to(lifecycle.state_root() / "data")


def test_spin_preserves_legacy_default_data_location(environment):
    s = lifecycle.Instance("spin")
    assert s.data == environment / "home" / ".simpleaudit-studio"
    assert s.db == s.data / "studio.sqlite3"


def test_reset_refuses_external_paths(environment, monkeypatch):
    custom = environment / "user-results"
    custom.mkdir()
    sentinel = custom / "keep.txt"
    sentinel.write_text("important")
    monkeypatch.setenv("SIMPLEAUDIT_DATA_DIR", str(custom))
    instance = lifecycle.Instance("spin")
    with pytest.raises(SystemExit, match="Custom data paths"):
        lifecycle.act(instance, "reset", yes=True)
    assert sentinel.read_text() == "important"


def test_uninstall_refuses_custom_sqlite_without_stopping(environment, monkeypatch):
    monkeypatch.setenv("SIMPLEAUDIT_SQLITE_PATH", str(environment / "shared.sqlite3"))
    instance = lifecycle.Instance("dev", environment / "checkout")
    with pytest.raises(SystemExit, match="Custom data paths"):
        lifecycle.act(instance, "uninstall", yes=True)


def test_delete_owned_data_only(environment):
    checkout = environment / "repo"
    checkout.mkdir()
    instance = lifecycle.Instance("dev", checkout)
    instance.data.mkdir(parents=True)
    (instance.data / "openwebui").mkdir()
    (instance.data / "openwebui" / "webui.db").write_text("fake")
    instance.db.write_text("fake")
    (checkout / "manage.py").write_text("safe")
    lifecycle._delete_owned(instance)
    assert not instance.data.exists()
    assert not instance.db.exists()
    assert (checkout / "manage.py").exists()


def test_default_port_selection_has_no_duplicate_ports():
    assert len(set(lifecycle._requested_ports(8000).values())) == 4
    assert lifecycle._requested_ports(8100)["public"] == 8100
    assert lifecycle._requested_ports(8100)["chat"] == 8180


def test_help_mentions_agent_info_and_commands(capsys):
    lifecycle.instructions("dev", agents=True)
    output = capsys.readouterr().out
    for expected in ("agent-info", "uninstall", "status", "down", "reset", "Never kill"):
        assert expected in output


def test_live_rejects_unowned_pid():
    assert not lifecycle._live({"pid": os.getpid(), "created": 1.0})
    assert not lifecycle._live({"pid": 999999999, "created": 0})


def test_uninstall_only_cleans_named_uv_packages(monkeypatch):
    calls = []
    monkeypatch.setattr(lifecycle.shutil, "which", lambda _: "/usr/bin/uv")
    class Result:
        returncode = 0
    monkeypatch.setattr(lifecycle.subprocess, "run", lambda args, **kwargs: (calls.append(args), Result())[1])
    lifecycle._uv_cleanup("dev")
    assert calls == [["/usr/bin/uv", "cache", "clean", "simpleaudit-studio"],
                     ["/usr/bin/uv", "cache", "clean", "open-webui"]]
    assert not any(args == ["/usr/bin/uv", "cache", "clean"] for args in calls)


def test_uninstall_cannot_use_live_external_db(environment, monkeypatch):
    monkeypatch.setenv("SIMPLEAUDIT_SQLITE_PATH", "/tmp/outside-repo.sqlite3")
    instance = lifecycle.Instance("dev", environment / "repo")
    with pytest.raises(SystemExit):
        lifecycle.act(instance, "uninstall", yes=True)
