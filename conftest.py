"""Pytest bootstrap for the SimpleAuditStudio Django test suite.

The existing tests are written as ``django.test.TestCase`` classes and are run
unchanged by pytest-django. The environment the settings module needs is set in
``pytest.ini`` (parsed before this file). This hook mirrors each test's Django
``@tag("...")`` values onto the pytest item as markers, so ``pytest -m "not
slow"`` works the same way ``manage.py test --exclude-tag slow`` does.
"""

import os

import pytest

from simpleaudit_studio import no_live_chat


def pytest_configure(config):
    # No test may reach a developer's live Open WebUI (an unmocked agent save
    # used to overwrite their real workspace models). See no_live_chat.py.
    no_live_chat.install()


def pytest_unconfigure(config):
    no_live_chat.uninstall()


#: pytest.ini's SIMPLEAUDIT_CHAT=off, as seen before any test ran.
_CHAT_ENV = os.environ.get("SIMPLEAUDIT_CHAT")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """A test that changes SIMPLEAUDIT_CHAT must restore it (``mock.patch.dict``).

    Popping it in a ``finally`` used to delete pytest.ini's ``off`` for every
    later test in the worker. Checked after every fixture (monkeypatch
    included) has torn down; put it back and fail the test that leaked.
    """
    result = yield
    leaked = os.environ.get("SIMPLEAUDIT_CHAT")
    if leaked == _CHAT_ENV:
        return result
    if _CHAT_ENV is None:
        os.environ.pop("SIMPLEAUDIT_CHAT", None)
    else:
        os.environ["SIMPLEAUDIT_CHAT"] = _CHAT_ENV
    pytest.fail(
        f"Test left SIMPLEAUDIT_CHAT={leaked!r} (expected {_CHAT_ENV!r}); "
        "restore it with mock.patch.dict(os.environ) or monkeypatch.",
        pytrace=False,
    )


@pytest.fixture(autouse=True)
def _synchronous_otlp_span_writes(settings):
    """Write OTLP spans at once: a background flush timer must not outlive a test."""
    settings.OTLP_SPAN_FLUSH_INTERVAL_S = 0


@pytest.fixture(autouse=True)
def _fresh_otlp_auth_cache():
    """Start each test with an empty OTLP auth cache.

    ``authenticate`` caches header -> credential for the process. A test's
    credentials are rolled back without the post_delete signal that clears
    it, so a later test in the same worker could be let in by a credential
    that no longer exists (an unauthenticated push answered 200, not 401).
    """
    from model_registry.otlp_services import clear_auth_cache

    clear_auth_cache()


@pytest.fixture(autouse=True)
def _no_openwebui_sync_throttle(settings):
    """Every Agent page load syncs in tests (ids are reused across tests)."""
    settings.OPENWEBUI_SYNC_TTL_S = 0


def pytest_collection_modifyitems(items):
    """Copy Django ``@tag`` values onto pytest items as markers.

    Django's ``tag()`` decorator stores tag names in ``unittest``'s
    ``_testcase_tags`` attribute on the class (and, when applied to a method,
    in the function's ``_testcase_tags``). We read that and attach a matching
    pytest marker to every test item, so ``-m`` selection and ``--testmon``
    both see the same grouping the Django runner uses.
    """
    for item in items:
        tags = set()
        # Class-level tags (e.g. @tag("slow") on a TestCase subclass).
        cls = getattr(item, "cls", None)
        if cls is not None:
            tags.update(getattr(cls, "_testcase_tags", ()))
        # Method-level tags.
        func = getattr(item, "function", None)
        if func is not None:
            tags.update(getattr(func, "_testcase_tags", ()))
        for tag in tags:
            item.add_marker(tag)
