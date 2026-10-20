"""The local bootstrap password must not drift between entry points.

Run:
    SIMPLEAUDIT_LOCAL_SQLITE=1 uv run manage.py test simpleaudit_studio.tests.test_bootstrap_defaults

``manage.py dev`` reads ``BOOTSTRAP_PASSWORD`` from ``.env.dev.example``;
the spin CLI falls back to a literal in code, and the Dockerfile bakes one
for the single-container/HF-Space mode. All three must agree on the same
default so that any local install logs in with the same credentials.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parents[2]


class BootstrapPasswordDefaultTests(SimpleTestCase):
    @staticmethod
    def _env_block_lines(lines: list[str], start: int) -> list[str]:
        """Collect one multi-line ENV instruction: the first line plus every
        backslash-continued line, ending with the final (uncontinued) entry."""
        block = [lines[start]]
        i = start + 1
        while i < len(lines) and lines[i].rstrip().endswith("\\"):
            block.append(lines[i])
            i += 1
        if i < len(lines) and lines[i].strip():
            block.append(lines[i])
        return block

    def _cli_fallbacks(self) -> list[str]:
        """Every ``os.environ.get("BOOTSTRAP_PASSWORD", <literal>)`` in the CLI."""
        source = (REPO_ROOT / "simpleaudit_studio" / "cli.py").read_text(encoding="utf-8")
        return re.findall(
            r'os\.environ\.get\(\s*"BOOTSTRAP_PASSWORD"\s*,\s*"([^"]+)"\s*\)', source
        )

    def test_cli_fallback_matches_dev_env_example(self):
        example = (REPO_ROOT / ".env.dev.example").read_text(encoding="utf-8")
        match = re.search(r"^BOOTSTRAP_PASSWORD=(\S+)", example, re.MULTILINE)
        self.assertIsNotNone(match, ".env.dev.example must define BOOTSTRAP_PASSWORD")
        expected = match.group(1)

        fallbacks = self._cli_fallbacks()
        self.assertTrue(fallbacks, "expected at least one BOOTSTRAP_PASSWORD fallback in cli.py")
        for fallback in fallbacks:
            self.assertEqual(fallback, expected)

    def test_dockerfile_bakes_the_same_default(self):
        """The single-container/HF-Space Dockerfile bakes the same default."""
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        # The baked defaults live in the ENV instruction that contains
        # BOOTSTRAP_PASSWORD; it is backslash-continued across lines.
        lines = dockerfile.splitlines()
        start = None
        for i, line in enumerate(lines):
            if not line.strip().startswith("ENV"):
                continue
            if "BOOTSTRAP_PASSWORD=" in "\n".join(self._env_block_lines(lines, i)):
                start = i
                break
        self.assertIsNotNone(start, "Dockerfile must bake BOOTSTRAP_PASSWORD in an ENV block")
        env_block = "\n".join(self._env_block_lines(lines, start))
        pw = re.search(r"BOOTSTRAP_PASSWORD=([^\s\\]+)", env_block)
        self.assertIsNotNone(pw, "Dockerfile ENV must set BOOTSTRAP_PASSWORD")
        example = (REPO_ROOT / ".env.dev.example").read_text(encoding="utf-8")
        expected = re.search(r"^BOOTSTRAP_PASSWORD=(\S+)", example, re.MULTILINE).group(1)
        self.assertEqual(pw.group(1), expected)

    def test_demo_password_default_matches(self):
        """config.settings DEMO_PASSWORD (login-form prefill in demo mode) agrees too."""
        settings_src = (REPO_ROOT / "config" / "settings.py").read_text(encoding="utf-8")
        m = re.search(r'DEMO_PASSWORD\s*=\s*os\.environ\.get\(\s*"DEMO_PASSWORD"\s*,\s*"([^"]+)"', settings_src)
        self.assertIsNotNone(m, "settings.py must define a DEMO_PASSWORD default")
        example = (REPO_ROOT / ".env.dev.example").read_text(encoding="utf-8")
        expected = re.search(r"^BOOTSTRAP_PASSWORD=(\S+)", example, re.MULTILINE).group(1)
        self.assertEqual(m.group(1), expected)
