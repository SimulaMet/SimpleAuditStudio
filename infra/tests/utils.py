"""Shared test helpers (use with infra/tests/factories.py)."""
import json
import os
from contextlib import contextmanager

from infra.tests.factories import UserFactory

PASSWORD = "testpass123"


def login(client, user, pw=PASSWORD):
    client.login(username=user.username, password=pw)


def superuser():
    """A superuser whose password is ``PASSWORD``."""
    user = UserFactory(is_superuser=True, is_staff=True)
    user.set_password(PASSWORD)
    user.save()
    return user


def post_json(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def patch_json(client, url, payload):
    return client.patch(url, data=json.dumps(payload), content_type="application/json")


@contextmanager
def safe_env():
    """Valid, non-default secrets so start-up checks pass on SQLite and Postgres."""
    values = {"DJANGO_SECRET_KEY": "test-secret-key-not-change-me", "POSTGRES_PASSWORD": PASSWORD}
    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
