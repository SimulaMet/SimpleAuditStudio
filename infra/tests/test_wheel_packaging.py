"""The wheel must ship every package and data dir the installed app needs.

Regression guard for the class of bug where a top-level directory (e.g. the
``prompts`` app, ``templates/``, ``static/``) is absent from
``[tool.hatch.build.targets.wheel] packages``: the wheel then builds fine but
the installed app 404s on templates or crashes on import.
"""

import tomllib
from pathlib import Path

from django.conf import settings

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: Non-Django-app dirs the installed app cannot run without: settings, the
#: ``spin``/``uvx`` CLI package, and the runtime data directories.
_ALWAYS_REQUIRED = {
    "config",
    "simpleaudit_studio",
    "templates",
    "static",
    "deploy",
}


def _wheel_packages() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return list(data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"])


def _root_app_packages() -> set[str]:
    """Root-level Django app packages (first dotted segment, present on disk)."""
    apps = set()
    for entry in settings.INSTALLED_APPS:
        root = entry.split(".")[0]
        if (REPO_ROOT / root).is_dir():
            apps.add(root)
    return apps


def test_wheel_packages_covers_all_runtime_dirs():
    packages = _wheel_packages()
    required = _ALWAYS_REQUIRED | _root_app_packages()
    missing = sorted(required - set(packages))
    assert not missing, (
        f"wheel packages list is missing runtime dirs {missing}; "
        "an installed (uvx / single-docker) app would not start"
    )


def test_wheel_packages_entries_all_exist():
    packages = _wheel_packages()
    stale = sorted(name for name in packages if not (REPO_ROOT / name).is_dir())
    assert not stale, f"wheel packages list references nonexistent dirs {stale}"
