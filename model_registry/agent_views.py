"""API views for Agent configuration CRUD."""
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


def _project(request):
    project = getattr(request, "project", None)
    if not project:
        raise StableAPIError("No active workspace.", status=400)
    return project


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
    profile = serializer.save(project=project)
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
    kb = serializer.save(project=project)
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
        return Response(serializer.data)

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
    tool = serializer.save(project=project)
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
        serializer.save()
        return Response(serializer.data)

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
        return Response(AgentDetailSerializer(agent_list, many=True).data)

    serializer = AgentSerializer(data=request.data, context={"project": project})
    serializer.is_valid(raise_exception=True)
    agent = serializer.save(project=project, created_by=request.user)
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
        return Response(AgentDetailSerializer(agent).data)

    if request.method == "PUT":
        serializer = AgentSerializer(agent, data=request.data, partial=True, context={"project": project})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(AgentDetailSerializer(agent).data)

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
