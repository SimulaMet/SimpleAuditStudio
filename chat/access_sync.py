"""Project/user/group projection and connection-level model ACLs.

Studio owns identity, memberships, and connection sharing. Open WebUI only
receives a generated projection: one group per workspace (plus an admin-only
group) and identical read grants on every model served by a connection.
"""
from __future__ import annotations

import logging

from chat.api import ChatAPI, ChatAPIError, chat_model_prefix

logger = logging.getLogger(__name__)
MARKER = "simpleaudit"


def _meta(project, kind: str) -> dict:
    return {MARKER: {"kind": kind, "studio_project_id": str(project.id), "schema": 1}}


def _data(project, kind: str) -> dict:
    # Open WebUI's current group endpoint persists custom data reliably; its
    # meta field is present in the schema but is dropped by some builds.
    return {"config": {"share": "members"}, MARKER: _meta(project, kind)[MARKER]}


def _ensure_group(api: ChatAPI, project, *, kind: str, field: str, name: str) -> str:
    group_id = getattr(project, field, None)
    if group_id:
        try:
            api.update_group(
                group_id, name, f"Studio {kind} group for {project.slug}.",
                data=_data(project, kind), meta=_meta(project, kind),
            )
            return group_id
        except ChatAPIError:
            logger.info("Open WebUI group %s disappeared; recreating it", group_id)

    marker = _meta(project, kind)[MARKER]
    for group in api.list_groups():
        if (
            (group.get("meta") or {}).get(MARKER) == marker
            or (group.get("data") or {}).get(MARKER) == marker
            or group.get("name") == name
        ):
            group_id = group.get("id")
            break
    else:
        group = api.create_group(
            name, f"Studio {kind} group for {project.slug}.",
            data=_data(project, kind), meta=_meta(project, kind),
        )
        group_id = group.get("id") if isinstance(group, dict) else None
    if not group_id:
        raise ChatAPIError(f"Open WebUI did not return a group id for {name!r}")
    if getattr(project, field, None) != group_id:
        setattr(project, field, group_id)
        project.save(update_fields=[field, "updated_at"])
    return group_id


def _sync_members(api: ChatAPI, group_id: str, desired: set[str]) -> None:
    current = set(api.group_user_ids(group_id))
    add = sorted(desired - current)
    remove = sorted(current - desired)
    if add:
        api.add_group_users(group_id, add)
    if remove:
        api.remove_group_users(group_id, remove)


def _user_ids(api_admin: ChatAPI) -> dict[int, str]:
    from accounts.models import User

    result: dict[int, str] = {}
    for user in User.objects.filter(is_active=True).order_by("id"):
        user_api = ChatAPI.as_user(user)
        response = user_api.sign_in()
        owui_id = response.get("id") if isinstance(response, dict) else None
        if owui_id and user.openwebui_user_id != owui_id:
            user.openwebui_user_id = owui_id
            user.save(update_fields=["openwebui_user_id"])
        if owui_id:
            user_api.update_current_user_settings({
                "ui": {"custom_metadata": {
                    MARKER: {"studio_user_id": str(user.id), "schema": 1},
                }},
            })
            result[user.id] = owui_id
    return result


def _connection_grants(connection, projects: dict[int, object]) -> list[dict[str, str]]:
    from model_registry.models import ModelConnection

    grants: set[tuple[str, str, str]] = set()
    grants.add(("group", projects[connection.project_id].openwebui_group_id, "read"))
    if connection.visibility == ModelConnection.Visibility.PUBLIC:
        grants = {("anyone", "*", "read")}
    elif connection.visibility == ModelConnection.Visibility.ADMINS:
        # Owner members always retain access. Other workspaces receive access
        # through their admin-only group, matching Studio's admin sharing rule.
        for project in projects.values():
            if any(m.role == "admin" for m in project.memberships.all()) and project.openwebui_admin_group_id:
                grants.add(("group", project.openwebui_admin_group_id, "read"))
    else:
        for project in connection.shared_with.all():
            if project.openwebui_group_id:
                grants.add(("group", project.openwebui_group_id, "read"))
    return [
        {"principal_type": principal, "principal_id": principal_id, "permission": permission}
        for principal, principal_id, permission in sorted(grants)
        if principal_id
    ]


def reconcile() -> dict[str, int]:
    """Reconcile Studio identities, groups, memberships, and model ACLs."""
    from accounts.models import Project, ProjectMembership, User
    from chat import config
    from model_registry.models import ModelConnection

    if not config.ENABLED:
        return {"users": 0, "groups": 0, "models": 0}
    admin = User.objects.filter(is_superuser=True, is_active=True).order_by("id").first()
    if admin is None:
        return {"users": 0, "groups": 0, "models": 0}
    api = ChatAPI.as_user(admin)
    ids = _user_ids(api)
    projects = {p.id: p for p in Project.objects.prefetch_related("memberships", "shared_connections").all()}
    groups = 0
    for project in projects.values():
        group_id = _ensure_group(api, project, kind="workspace", field="openwebui_group_id", name=f"Studio — {project.name}")
        admin_group_id = _ensure_group(api, project, kind="workspace_admins", field="openwebui_admin_group_id", name=f"Studio — {project.name} — Admins")
        members = {ids[m.user_id] for m in project.memberships.all() if m.user_id in ids}
        admins = {ids[m.user_id] for m in project.memberships.all() if m.user_id in ids and m.role == ProjectMembership.Role.ADMIN}
        _sync_members(api, group_id, members)
        _sync_members(api, admin_group_id, admins)
        groups += 2

    model_count = 0
    for connection in ModelConnection.objects.prefetch_related("models", "shared_with").all():
        grants = _connection_grants(connection, projects)
        for model in connection.models.all():
            api.update_model_access(
                f"{chat_model_prefix(connection)}.{model.model_id}",
                grants,
                name=model.display_name or model.model_id,
            )
            model_count += 1
    return {"users": len(ids), "groups": groups, "models": model_count}


def reconcile_safely() -> dict[str, int]:
    try:
        return reconcile()
    except Exception as exc:  # noqa: BLE001 - access sync must not break Studio
        logger.warning("Open WebUI access reconciliation skipped: %s", exc)
        return {"users": 0, "groups": 0, "models": 0}
