"""Pytest bootstrap for the SimpleAuditStudio Django test suite.

The existing tests are written as ``django.test.TestCase`` classes and are run
unchanged by pytest-django. The environment the settings module needs is set in
``pytest.ini`` (parsed before this file). This hook mirrors each test's Django
``@tag("...")`` values onto the pytest item as markers, so ``pytest -m "not
slow"`` works the same way ``manage.py test --exclude-tag slow`` does.
"""


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
