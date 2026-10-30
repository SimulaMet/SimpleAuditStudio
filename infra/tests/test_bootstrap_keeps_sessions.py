"""Restarting the web container re-runs bootstrap; it must not invalidate sessions."""

import pytest

from accounts.services import bootstrap_admin_and_default_project


@pytest.mark.django_db
def test_rebootstrap_with_same_password_keeps_password_hash():
    user, _ = bootstrap_admin_and_default_project(username="studio", email="a@x.test", password="pw-one")
    before = user.password
    user, _ = bootstrap_admin_and_default_project(username="studio", email="a@x.test", password="pw-one")
    assert user.password == before


@pytest.mark.django_db
def test_rebootstrap_corrects_a_changed_password():
    bootstrap_admin_and_default_project(username="studio", email="a@x.test", password="pw-one")
    user, _ = bootstrap_admin_and_default_project(username="studio", email="a@x.test", password="pw-two")
    assert user.check_password("pw-two")
