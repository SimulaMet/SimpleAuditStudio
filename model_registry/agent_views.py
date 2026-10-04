"""API views for Agent configuration CRUD.

Open WebUI sync: Studio rows are the durable reference; Open WebUI is the
source of truth for the live agent/KB/tool. Every create/update below pushes
to Open WebUI (best-effort, no-op when chat is disabled) and every delete
propagates before the local row goes.
"""
import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from infra.exceptions import StableAPIError

from .agent_serializers import (
    AgentDetailSerializer,
    AgentSerializer,
    KnowledgeBaseSerializer,
    RetrievalProfileSerializer,
    ToolSerializer,
)
from .models import (
    Agent,
    KnowledgeBase,
    RetrievalProfile,
    Tool,
)
from .services import (
    delete_agent_from_openwebui,
    delete_knowledge_base_from_openwebui,
    delete_tool_from_openwebui,
    sync_agent_to_openwebui,
    sync_knowledge_base_to_openwebui,
    sync_tool_to_openwebui,
)

logger = logging.getLogger(__name__)


def _project(request):
    project = getattr(request, "project", None)
    if not project:
        raise StableAPIError("No active workspace.", status=400)
    return project


def _remote_tool_content(tool) -> str:
    """A tool's current Open WebUI source, or "" when unavailable.

    A metadata-only Studio edit must not wipe the tool's code, so the update
    re-sends the untouched remote content.
    """
    from chat import config as chat_config

    if not chat_config.ENABLED or not tool.external_id:
        return ""
    try:
        from chat.api import ChatAPI

        api = ChatAPI.as_user(tool.created_by or _any_authenticated_user())
        return api.get_tool(tool.external_id).get("content") or ""
    except Exception:  # noqa: BLE001 - a flaky OpenWebUI must not break a Studio edit
        return ""


def _any_authenticated_user():
    from django.contrib.auth import get_user_model

    return get_user_model().objects.order_by("id").first()


# --- Retrieval Profiles -----------------------------------------------------


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def retrieval_profiles(request):
    project = _project(request)
    if request.method == "GET":
        profiles = RetrievalProfile.objects.filter(project=project)
        return Response(RetrievalProfileSerializer(profiles, many=True).data)

    serializer = RetrievalProfileSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    profile = serializer.save(project=project, created_by=request.user)
    return Response(RetrievalProfileSerializer(profile).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([IsAuthenticated])
def retrieval_profile_detail(request, pk):
    project = _project(request)
    profile = RetrievalProfile.objects.filter(pk=pk, project=project).first()
    if not profile:
        raise StableAPIError("Retrieval profile not found.", status=404)

    if request.method == "GET":
        return Response(RetrievalProfileSerializer(profile).data)

    if request.method == "PUT":
        serializer = RetrievalProfileSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    profile.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# --- Knowledge Bases --------------------------------------------------------


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def knowledge_bases(request):
    project = _project(request)
    if request.method == "GET":
        kbs = KnowledgeBase.objects.filter(project=project)
        return Response(KnowledgeBaseSerializer(kbs, many=True).data)

    serializer = KnowledgeBaseSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    kb = serializer.save(project=project, created_by=request.user)
    sync_knowledge_base_to_openwebui(kb, request.user)
    return Response(KnowledgeBaseSerializer(kb).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([IsAuthenticated])
def knowledge_base_detail(request, pk):
    project = _project(request)
    kb = KnowledgeBase.objects.filter(pk=pk, project=project).first()
    if not kb:
        raise StableAPIError("Knowledge base not found.", status=404)

    if request.method == "GET":
        return Response(KnowledgeBaseSerializer(kb).data)

    if request.method == "PUT":
        serializer = KnowledgeBaseSerializer(kb, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        sync_knowledge_base_to_openwebui(kb, request.user)
        return Response(serializer.data)

    delete_knowledge_base_from_openwebui(kb)
    kb.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# --- Tools ------------------------------------------------------------------


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def tools(request):
    project = _project(request)
    if request.method == "GET":
        tool_list = Tool.objects.filter(project=project)
        return Response(ToolSerializer(tool_list, many=True).data)

    serializer = ToolSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    content = serializer.validated_data.pop("content", "")
    tool = serializer.save(project=project, created_by=request.user)
    if content:
        # Open WebUI owns tool source; only push when the caller supplied it.
        sync_tool_to_openwebui(tool, request.user, content=content)
    return Response(ToolSerializer(tool).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([IsAuthenticated])
def tool_detail(request, pk):
    project = _project(request)
    tool = Tool.objects.filter(pk=pk, project=project).first()
    if not tool:
        raise StableAPIError("Tool not found.", status=404)

    if request.method == "GET":
        return Response(ToolSerializer(tool).data)

    if request.method == "PUT":
        serializer = ToolSerializer(tool, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        content = serializer.validated_data.pop("content", "")
        serializer.save()
        if tool.external_id:
            if not content:
                # Metadata-only edit: re-push the untouched remote source.
                content = _remote_tool_content(tool)
            if content:
                sync_tool_to_openwebui(tool, request.user, content=content)
        return Response(serializer.data)

    delete_tool_from_openwebui(tool)
    tool.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# --- Agents -----------------------------------------------------------------


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def agents(request):
    project = _project(request)
    if request.method == "GET":
        agent_list = Agent.objects.filter(project=project).select_related(
            "base_model", "base_model__connection", "retrieval_profile"
        ).prefetch_related("knowledge_bases", "tools")
        return Response(
            AgentDetailSerializer(agent_list, many=True, context={"request": request}).data
        )

    serializer = AgentSerializer(data=request.data, context={"project": project})
    serializer.is_valid(raise_exception=True)
    agent = serializer.save(project=project, created_by=request.user)
    sync_agent_to_openwebui(agent, request.user)
    return Response(AgentDetailSerializer(agent).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([IsAuthenticated])
def agent_detail(request, pk):
    project = _project(request)
    agent = Agent.objects.filter(pk=pk, project=project).select_related(
        "base_model", "base_model__connection", "retrieval_profile"
    ).prefetch_related("knowledge_bases", "tools").first()
    if not agent:
        raise StableAPIError("Agent not found.", status=404)

    if request.method == "GET":
        return Response(
            AgentDetailSerializer(
                agent, context={"request": request, "fetch_live": True}
            ).data
        )

    if request.method == "PUT":
        serializer = AgentSerializer(agent, data=request.data, partial=True, context={"project": project})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        sync_agent_to_openwebui(agent, request.user)
        return Response(AgentDetailSerializer(agent, context={"request": request}).data)

    delete_agent_from_openwebui(agent)
    agent.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def agent_snapshot(request, pk):
    """Return the configuration snapshot for an agent (used by audit runs)."""
    project = _project(request)
    agent = Agent.objects.filter(pk=pk, project=project).first()
    if not agent:
        raise StableAPIError("Agent not found.", status=404)
    return Response(agent.config_snapshot())
