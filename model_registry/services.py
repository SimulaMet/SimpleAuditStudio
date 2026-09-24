"""Services for the model registry."""
from django.db import IntegrityError, transaction

from infra.exceptions import StableAPIError
from model_registry.models import ModelEndpoint
from accounts.models import Project
from scenarios.services import require_project_role


def _validate_secret_reference(secret_reference: str) -> str:
    normalized = (secret_reference or "").strip()
    if not normalized:
        return ""
    if not all(char.isalnum() or char in {"_", "-"} for char in normalized):
        raise StableAPIError(detail="Secret reference must be an environment-style identifier.", code="invalid_secret_reference")
    return normalized


@transaction.atomic
def create_model_endpoint(*, project: Project, user, display_name: str, provider: str, base_url: str, model_id: str, model_revision: str = "", capabilities: dict | None = None, default_parameters: dict | None = None, secret_reference: str = "") -> ModelEndpoint:
    require_project_role(user, project)
    try:
        return ModelEndpoint.objects.create(
            project=project,
            display_name=display_name.strip(),
            provider=provider.strip(),
            base_url=base_url.strip(),
            model_id=model_id.strip(),
            model_revision=model_revision.strip(),
            capabilities=capabilities or {},
            default_parameters=default_parameters or {},
            secret_reference=_validate_secret_reference(secret_reference),
            created_by=user,
        )
    except IntegrityError as exc:
        # (project_id, display_name) is unique. Surface a clean 409 rather than
        # leaking a raw 500 to the client.
        raise StableAPIError(detail="A model endpoint with this display name already exists in the project.", code="duplicate_display_name", http_status=409) from exc

