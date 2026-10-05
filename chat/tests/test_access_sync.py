from unittest import mock

from django.test import TestCase

from accounts.models import ProjectMembership
from chat import access_sync
from infra.tests.factories import (
    MembershipFactory,
    ModelConnectionFactory,
    ProjectFactory,
    RegisteredModelFactory,
    UserFactory,
)


class AccessSyncTests(TestCase):
    def test_projects_and_connections_project_to_openwebui(self):
        admin = UserFactory(is_superuser=True, is_staff=True)
        project = ProjectFactory()
        MembershipFactory(project=project, user=admin, role=ProjectMembership.Role.ADMIN)
        connection = ModelConnectionFactory(project=project, name="Shared provider")
        model = RegisteredModelFactory(connection=connection, project=project, model_id="default")

        api = mock.Mock()
        api.sign_in.return_value = {"id": "owui-user-1"}
        api.list_groups.return_value = []
        api.create_group.side_effect = [
            {"id": "owui-workspace-1"},
            {"id": "owui-admins-1"},
        ]
        api.group_user_ids.return_value = []

        with mock.patch("chat.config.ENABLED", True), mock.patch(
            "chat.access_sync.ChatAPI.as_user", return_value=api
        ):
            result = access_sync.reconcile()

        self.assertEqual(result["users"], 1)
        self.assertEqual(result["groups"], 2)
        self.assertEqual(result["models"], 1)
        project.refresh_from_db()
        self.assertEqual(project.openwebui_group_id, "owui-workspace-1")
        self.assertEqual(project.openwebui_admin_group_id, "owui-admins-1")
        api.add_group_users.assert_any_call("owui-workspace-1", ["owui-user-1"])
        api.add_group_users.assert_any_call("owui-admins-1", ["owui-user-1"])
        api.update_model_access.assert_called_once_with(
            f"{connection.id}.default",
            [{
                "principal_type": "group",
                "principal_id": "owui-workspace-1",
                "permission": "read",
            }],
            name=model.display_name,
        )
