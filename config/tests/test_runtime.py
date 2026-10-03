"""Unit tests for config.runtime.resolve_mode().

Plain ``unittest`` (no DB) so they run under both pytest and
``manage.py test``. Each test scrubs the mode-relevant env vars, sets the
inputs for its case, and restores them afterwards.
"""
from __future__ import annotations

import os
import unittest

from config import runtime
from config.runtime import (
    VALID_MODES,
    UnknownModeError,
    profile_for,
    resolve_mode,
)

#: Every env var that can steer resolution — cleared per-test so a leftover
#: real environment (e.g. a .env) cannot leak in.
_MODE_VARS = (
    "SIMPLEAUDIT_MODE",
    "SIMPLEAUDIT_MINIMAL",
    "SIMPLEAUDIT_LOCAL_SQLITE",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "HATCHET_SERVER_URL",
)


class ResolveModeTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _MODE_VARS}
        for k in _MODE_VARS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # --- explicit SIMPLEAUDIT_MODE wins -------------------------------
    def test_explicit_mode_wins_over_inference(self):
        os.environ["SIMPLEAUDIT_MINIMAL"] = "1"  # would infer embedded
        os.environ["SIMPLEAUDIT_MODE"] = "compose"
        self.assertEqual(resolve_mode().id, "compose")

    def test_each_valid_mode_resolves(self):
        for mode in VALID_MODES:
            os.environ["SIMPLEAUDIT_MODE"] = mode
            self.assertEqual(resolve_mode().id, mode)

    def test_mode_is_case_and_whitespace_insensitive(self):
        os.environ["SIMPLEAUDIT_MODE"] = "  Dev "
        self.assertEqual(resolve_mode().id, "dev")

    def test_unknown_explicit_mode_raises(self):
        os.environ["SIMPLEAUDIT_MODE"] = "warp"
        with self.assertRaises(UnknownModeError):
            resolve_mode()

    # --- inference ----------------------------------------------------
    def test_minimal_infers_embedded(self):
        os.environ["SIMPLEAUDIT_MINIMAL"] = "1"
        p = resolve_mode()
        self.assertEqual(p.id, "embedded")
        self.assertEqual(p.database, "sqlite")
        self.assertEqual(p.queue, "embedded")
        self.assertFalse(p.debug)

    def test_no_minimal_no_mode_raises(self):
        # The legacy "just read whatever is in .env" path is gone.
        with self.assertRaises(UnknownModeError):
            resolve_mode()

    def test_compose_inferred_from_pg_and_hatchet(self):
        os.environ["POSTGRES_HOST"] = "postgres"
        os.environ["POSTGRES_PORT"] = "5432"
        os.environ["HATCHET_SERVER_URL"] = "http://postgres:8888"
        self.assertEqual(resolve_mode().id, "compose")

    def test_compose_not_inferred_when_sqlite(self):
        # .env.local.example carries POSTGRES_* + HATCHET_* but SQLite wins.
        os.environ["SIMPLEAUDIT_LOCAL_SQLITE"] = "1"
        os.environ["POSTGRES_HOST"] = "localhost"
        os.environ["POSTGRES_PORT"] = "5432"
        os.environ["HATCHET_SERVER_URL"] = "http://localhost:8888"
        with self.assertRaises(UnknownModeError):
            resolve_mode()

    def test_compose_not_inferred_without_hatchet(self):
        os.environ["POSTGRES_HOST"] = "localhost"
        os.environ["POSTGRES_PORT"] = "5432"
        with self.assertRaises(UnknownModeError):
            resolve_mode()

    # --- profile shape ------------------------------------------------
    def test_dev_profile_shape(self):
        p = profile_for("dev")
        self.assertEqual(p.process, "single")
        self.assertEqual(p.database, "sqlite")
        self.assertEqual(p.queue, "embedded")
        self.assertEqual(p.chat, "on")
        self.assertTrue(p.debug)

    def test_compose_profile_shape(self):
        p = profile_for("compose")
        self.assertEqual(p.process, "multi-container")
        self.assertEqual(p.database, "postgres")
        self.assertEqual(p.queue, "external")
        self.assertEqual(p.chat, "on")
        self.assertEqual(p.chat_mode, "docker")
        self.assertFalse(p.debug)

    def test_as_dict(self):
        d = profile_for("dev").as_dict()
        self.assertEqual(d["mode"], "dev")
        self.assertEqual(d["database"], "sqlite")
        self.assertTrue(d["debug"])


class SingleProcessRunTests(unittest.TestCase):
    """manage.py pre-setup detection: which runs get SQLite + chat defaults."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("SIMPLEAUDIT_MODE",)}
        os.environ.pop("SIMPLEAUDIT_MODE", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_dev_command(self):
        self.assertTrue(runtime.is_single_process_run("dev", ["manage.py", "dev"]))

    def test_dev_server_embedded_flag(self):
        self.assertTrue(
            runtime.is_single_process_run(
                "dev_server", ["manage.py", "dev_server", "--embedded"]
            )
        )

    def test_env_mode_dev(self):
        # The env spelling must match the flag spelling exactly.
        os.environ["SIMPLEAUDIT_MODE"] = "dev"
        self.assertTrue(runtime.is_single_process_run("dev_server", ["manage.py", "dev_server"]))

    def test_env_mode_embedded(self):
        os.environ["SIMPLEAUDIT_MODE"] = "embedded"
        self.assertTrue(runtime.is_single_process_run("dev_server", ["manage.py", "dev_server"]))

    def test_env_mode_case_insensitive(self):
        os.environ["SIMPLEAUDIT_MODE"] = "  Dev "
        self.assertTrue(runtime.is_single_process_run("dev_server", ["manage.py", "dev_server"]))

    def test_container_mode_env_not_single_process(self):
        os.environ["SIMPLEAUDIT_MODE"] = "compose"
        self.assertFalse(runtime.is_single_process_run("dev_server", ["manage.py", "dev_server"]))
        os.environ["SIMPLEAUDIT_MODE"] = "single-docker"
        self.assertFalse(runtime.is_single_process_run("dev_server", ["manage.py", "dev_server"]))

    def test_bare_dev_server_not_single_process(self):
        self.assertFalse(runtime.is_single_process_run("dev_server", ["manage.py", "dev_server"]))

    def test_other_commands_not_single_process(self):
        self.assertFalse(runtime.is_single_process_run("migrate", ["manage.py", "migrate"]))
        self.assertFalse(runtime.is_single_process_run(None, ["manage.py"]))

    def test_chat_disable_flag_variants(self):
        self.assertTrue(runtime.has_chat_disable_flag(["dev", "--disable-chat"]))
        self.assertTrue(runtime.has_chat_disable_flag(["dev", "--no-chat"]))
        self.assertFalse(runtime.has_chat_disable_flag(["dev", "--no-worker"]))
        self.assertFalse(runtime.has_chat_disable_flag(["dev"]))


class ChatPrecedenceTests(unittest.TestCase):
    """Flag > explicit env value > mode default (chat on for single-process)."""

    def test_mode_default_on_for_single_process(self):
        self.assertEqual(runtime.effective_chat_mode("dev"), "embedded")
        self.assertEqual(runtime.effective_chat_mode("embedded"), "embedded")
        self.assertEqual(runtime.effective_chat_mode("single-docker"), "embedded")

    def test_mode_default_on_for_compose(self):
        # Compose chat runs as the `docker` chat mode (web serves /chat/,
        # open-webui + chat-proxy containers behind it).
        self.assertEqual(runtime.effective_chat_mode("compose"), "docker")

    def test_disable_flag_beats_env_and_default(self):
        # The flag is an explicit opt-out and wins over a .env that pins chat
        # on — otherwise --disable-chat would be a no-op on this machine.
        os.environ["SIMPLEAUDIT_CHAT"] = "embedded"
        try:
            self.assertEqual(runtime.effective_chat_mode("dev", disable_flag=True), "off")
        finally:
            os.environ.pop("SIMPLEAUDIT_CHAT", None)

    def test_disable_flag_beats_compose_default_on(self):
        # The flag wins over compose's on-default: off.
        self.assertEqual(runtime.effective_chat_mode("compose", disable_flag=True), "off")

    def test_explicit_env_wins_over_default_without_flag(self):
        # No flag: an explicit env value is honored, on or off.
        os.environ["SIMPLEAUDIT_CHAT"] = "docker"
        try:
            self.assertEqual(runtime.effective_chat_mode("dev"), "docker")
        finally:
            os.environ.pop("SIMPLEAUDIT_CHAT", None)
        os.environ["SIMPLEAUDIT_CHAT"] = "off"
        try:
            self.assertEqual(runtime.effective_chat_mode("dev"), "off")
        finally:
            os.environ.pop("SIMPLEAUDIT_CHAT", None)


if __name__ == "__main__":
    unittest.main()
