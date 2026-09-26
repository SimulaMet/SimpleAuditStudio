# Super Admin Dashboard + Profile — Design

Date: 2026-09-26
Status: Approved

## Problem

SimpleAudit Studio has no platform-level administration. A super admin
(`is_superuser`) today gets a global content bypass — they can open any
workspace and see its full content — but there is no page to manage workspaces
or users, no aggregate usage view, and no way for any user to update their own
profile.

This design adds:

1. A **Super Admin page** (`/admin-settings/`) reachable from an "Admin
   Settings" item in the lower sidebar region, visible to superusers only, with
   three tabs: **Overview** (aggregate stats), **Workspaces** (CRUD + member
   assignment), **Users** (full CRUD).
2. A **self-service Profile page** (`/profile/`) for all users.
3. An **archive** lifecycle for workspaces: a workspace with data is archived
   (read-only for its members) rather than deleted; an empty workspace is
   hard-deleted.
4. A **membership-based content-access model**: a super admin sees full content
   only for workspaces they are a member of; for all other workspaces they see
   aggregate counts only.

## Goals

- Let a super admin manage workspaces and users from one place.
- Let a super admin review platform usage (counts) without exposing any
  workspace's actual content they are not part of.
- Let every user update their own profile (name, email, username, password).
- Preserve audit history when users or workspaces are removed.

## Non-Goals

- Billing, invitations by email, or SSO-specific admin flows.
- A separate SPA/admin frontend.
- Role-based access beyond the existing `admin`/`auditor`/`viewer` workspace
  roles and the `is_superuser` flag.

## Confirmed Decisions

| Decision | Choice |
|---|---|
| Profile scope | Self-service profile for all users AND super admin edits any user from the admin page |
| Stats visibility | Counts only — no content drill-down for non-member workspaces |
| User management | Full CRUD: create, edit, deactivate, delete |
| Workspace delete | Delete only if empty, else archive; archived badge visible in members' workspace picker |
| Archived workspace access | Read-only for members (can view, cannot mutate); super admin can still manage from the admin page |
| Page structure | Single page `/admin-settings/` with tabs: Overview / Workspaces / Users |
| Profile entry | New "Profile" nav item in the sidebar (all users) |
| Super admin toggle | Yes, from the Users tab, guarded (≥1 super admin always remains; cannot demote self) |
| Content access model | Super admin sees full content only for workspaces they are a member of; aggregates only otherwise |

## Architecture

Server-rendered Django, consistent with the existing codebase: class-based
views in `infra/ui.py`, DRF API endpoints in `accounts/views.py`, HTMX/fetch +
Tailwind (CDN) in templates. No new frontend stack, no new build tooling.

### Data model

`accounts.models.Project` gains:

```python
archived = models.BooleanField(default=False, db_index=True)
```

This mirrors the existing `AuditRun.archived` soft-hide pattern. A migration
`accounts/migrations/000X_project_archived.py` adds the column.

### Permission model

The central change is that **superuser content access becomes
membership-based** instead of a global bypass.

- `accounts.services.ensure_project_access(user, project)`:
  - Anonymous → `False`.
  - `DEFAULT_PROJECT_SLUG` → `True` (unchanged; the shared workspace is visible
    to everyone).
  - Otherwise → `True` iff the user has a `ProjectMembership` for the project.
    A superuser is **not** automatically granted access to every project; they
    must be a member (or rely on the Default workspace).
- `infra.context_processors.workspaces`: the sidebar switcher lists only the
  workspaces the user is a member of (plus Default). Superusers no longer see
  every workspace in the normal app — they see all of them only in the Admin
  page.
- `accounts.views.list_workspaces` and `infra.ui.WorkspacesView`: same
  membership-based listing.
- New helper `accounts.services.require_project_writable(user, project)`:
  raises `StableAPIError(code="workspace_archived", http_status=403)` when
  `project.archived` is true and the user is not a superuser. Called from every
  mutation path so both the UI and the API enforce read-only on archived
  workspaces.
- New `infra.ui.SuperuserRequiredMixin`: restricts a view to `is_superuser`
  only (distinct from the existing `AdminRequiredMixin`, which also admits
  workspace admins).

Superusers keep full **management** rights (the Admin page, user CRUD,
workspace archive/delete, member management on any workspace) — what changes is
**content** access, which is now membership-gated.

## Admin page (`/admin-settings/`)

Django's built-in admin occupies `/admin/`, so the new page lives at
`/admin-settings/`. The sidebar nav label is "Admin Settings", rendered in the
lower region (above the user row) and gated on `is_superuser`.

`infra.ui.AdminView` is a `SuperuserRequiredMixin + TemplateView` with
`template_name = "admin.html"`. The active tab is selected by `?tab=overview`
(default), `?tab=workspaces`, or `?tab=users`.

### Overview tab

- Platform totals: workspaces (active / archived), users (active / inactive),
  audit runs (total / completed / failed / active), scenarios, model endpoints,
  model connections.
- Per-workspace table: name, member count, audit runs (total / completed /
  failed), scenario count, model endpoint count, connection count, status
  (Active / Archived), created date. **Counts only — no links into workspace
  content.**

### Workspaces tab

Table of all workspaces with actions:

- **Archive** — available when the workspace has data.
- **Delete** — available only when the workspace is empty.
- **Unarchive** — available for archived workspaces.
- **Rename / edit description.**
- **Manage members** — add a user by username with a role, change a role,
  remove a member. Reuses the existing member APIs, which already admit
  superusers on any workspace.
- Destructive actions (archive, delete) show a confirmation dialog.
- Archived workspaces show an "Archived" badge.

### Users tab

Table of all users: username, email, name, status (active / inactive),
super-admin badge, created date. Actions:

- **Create user** — username, email, password.
- **Edit** — first/last name, email.
- **Reset password.**
- **Activate / Deactivate.**
- **Toggle super admin** — guarded: cannot demote the last super admin, cannot
  demote self.
- **Delete** — guarded: cannot delete self. Deleting a user removes their
  memberships (cascade) and nulls `AuditRun.created_by` (existing `SET_NULL`),
  preserving audit history.

## Profile page (`/profile/`)

`infra.ui.ProfileView` is a `LoginRequiredMixin + TemplateView` with
`template_name = "profile.html"`. A "Profile" nav item is added to the sidebar
for all authenticated users.

Editable: first name, last name, email (uniqueness-validated), username
(uniqueness-validated). Password change requires the current password plus a
new password validated by Django's password validators. Read-only display:
account creation date and a super-admin badge where applicable.

## API surface

New routes in `accounts/admin_urls.py`, included at `api/admin/` in
`config/urls.py`, all superuser-only:

- `GET /api/admin/stats/` — platform + per-workspace counts.
- `POST /api/admin/workspaces/<id>/archive/`
- `POST /api/admin/workspaces/<id>/unarchive/`
- `POST /api/admin/users/` — create user.
- `PATCH /api/admin/users/<id>/` — edit name/email, activate/deactivate, toggle
  super admin, reset password (with guards).
- `DELETE /api/admin/users/<id>/` — delete user + memberships (not self).

New self route in `accounts/auth_urls.py`:

- `PATCH /api/auth/profile/` — update own profile / password.

Existing endpoints updated:

- `accounts.views.workspace_detail` DELETE keeps the "delete only if empty"
  rule (already in `delete_workspace`); a non-empty workspace returns a 409
  hinting to archive.
- Member endpoints block mutations on archived workspaces for non-superusers.
- Workspace serializers gain an `archived` field for the picker badge.

## Archived-workspace read-only enforcement

`require_project_writable` is called from every mutation path:

- `infra/ui.py`: `NewAuditView.post`, scenario create/edit/delete/revert/
  import/set-create/set-delete/set-rename, model delete, connection delete,
  audit cancel/archive/rename, workspace manage actions.
- `audits/services.py`: `create_audit_run`, `submit_audit_run`.
- `scenarios/services.py`: mutation functions.
- `accounts/services.py`: `update_workspace`, `delete_workspace`, member
  add/remove/role (non-superuser).

View access (dashboard, audit detail, scenarios list, models list) remains
allowed for members of an archived workspace — only mutations are blocked. The
workspace picker and the workspaces page show an "Archived" badge.

## Error handling

All API errors use the existing `StableAPIError` with stable codes:
`workspace_archived` (403), `workspace_not_empty` (409), `user_not_found`
(404), `cannot_modify_superuser` (403), `last_admin` (409),
`last_superuser` (409), `cannot_delete_self` (409), `invalid_credentials`
(401). The UI surfaces these via the existing messages framework and inline
error elements.

## Testing

New modules under `infra/tests/`, using the existing `pytest-django` +
factory setup in `infra/tests/factories.py`:

- `test_admin_page.py` — page renders, tab context, 403 for non-superusers.
- `test_admin_api.py` — stats, user CRUD, archive/unarchive, guards.
- `test_profile.py` — self profile update, password change, validation.
- `test_workspace_archive.py` — archive read-only enforcement across mutation
  paths, badge visibility.
- Extend `test_smoke_all_pages.py` for the new pages.

Note: `MembershipFactory` currently sets `role="owner"`, which is not a valid
`ProjectMembership.Role` choice; this is corrected when extending tests.

## Files touched

| File | Change |
|---|---|
| `accounts/models.py` | `Project.archived` field |
| `accounts/migrations/000X_project_archived.py` | New migration |
| `accounts/services.py` | Membership-based superuser access, `require_project_writable`, archive/unarchive services, user CRUD services |
| `accounts/serializers.py` | `ProfileUpdateSerializer`, `UserCreateSerializer`, `UserAdminUpdateSerializer`, `archived` on workspace serializers |
| `accounts/views.py` | Profile API, admin stats/users APIs, archive-aware workspace delete, membership-based listing |
| `accounts/admin_urls.py` | New — admin API routes |
| `accounts/auth_urls.py` | Add profile route |
| `config/urls.py` | `/admin-settings/`, `/profile/`, `api/admin/` includes |
| `infra/ui.py` | `AdminView`, `ProfileView`, `SuperuserRequiredMixin`, archived-write guards on mutation views |
| `infra/context_processors.py` | `is_superuser` flag, membership-based workspace list, `archived` in picker items |
| `audits/services.py` | Archived-write guard on run creation/submission |
| `scenarios/services.py` | Archived-write guard on mutations |
| `templates/admin.html` | New — tabbed admin page |
| `templates/profile.html` | New — profile page |
| `templates/base.html` | "Admin Settings" nav (lower region, superuser only) + "Profile" nav item |
| `templates/workspaces.html` | Archived badge |
| `infra/tests/test_admin_page.py` | New |
| `infra/tests/test_admin_api.py` | New |
| `infra/tests/test_profile.py` | New |
| `infra/tests/test_workspace_archive.py` | New |
| `infra/tests/test_smoke_all_pages.py` | Extend for new pages |
