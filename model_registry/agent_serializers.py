"""DRF serializers for the Agent configuration domain."""
from rest_framework import serializers

from model_registry.models import (
    Agent,
    KnowledgeBase,
    RegisteredModel,
    RetrievalProfile,
    Tool,
)


class RetrievalProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = RetrievalProfile
        fields = [
            "id", "name", "search_mode", "top_k",
            "rerank_enabled", "rerank_top_k", "relevance_threshold",
            "bm25_weight", "full_context",
        ]
        read_only_fields = ["id"]


class KnowledgeBaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeBase
        fields = [
            "id", "name", "external_id", "description", "authority",
            "trust_level", "sensitivity", "version", "valid_from",
            "valid_until", "enabled",
        ]
        read_only_fields = ["id"]


class ToolSerializer(serializers.ModelSerializer):
    # Open WebUI owns the tool's Python source; Studio never stores it.
    # Supplied on create (and optionally on edit) and pushed straight through.
    content = serializers.CharField(
        required=False, allow_blank=True, write_only=True, default="",
    )

    class Meta:
        model = Tool
        fields = [
            "id", "name", "type", "external_id", "description",
            "input_schema", "output_schema",
            "read_only", "has_side_effects", "external_network",
            "handles_sensitive_data", "enabled", "content",
        ]
        read_only_fields = ["id", "external_id"]


class AgentSerializer(serializers.ModelSerializer):
    """Create/update an Agent.

    ``base_model`` is a primary-key reference to an existing ``RegisteredModel``.
    Knowledge bases and tools are sets of primary keys.
    """

    base_model = serializers.PrimaryKeyRelatedField(
        queryset=RegisteredModel.objects.filter(enabled=True),
    )
    knowledge_bases = serializers.PrimaryKeyRelatedField(
        queryset=KnowledgeBase.objects.filter(enabled=True),
        many=True,
        required=False,
    )
    tools = serializers.PrimaryKeyRelatedField(
        queryset=Tool.objects.filter(enabled=True),
        many=True,
        required=False,
    )
    retrieval_profile = serializers.PrimaryKeyRelatedField(
        queryset=RetrievalProfile.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = Agent
        fields = [
            "id", "name", "description", "base_model", "system_prompt",
            "knowledge_bases", "tools", "retrieval_profile",
            "capabilities", "metadata", "enabled", "external_id",
        ]
        read_only_fields = ["id", "external_id"]

    def validate(self, attrs):
        project = self.context.get("project")
        if project:
            model = attrs.get("base_model") or (self.instance.base_model if self.instance else None)
            if model and model.project_id != project.id:
                raise serializers.ValidationError(
                    {"base_model": "Model must belong to the same workspace."}
                )
            for field in ("knowledge_bases", "tools"):
                items = attrs.get(field)
                if items:
                    for item in items:
                        if item.project_id != project.id:
                            raise serializers.ValidationError(
                                {field: f"{item.name} does not belong to this workspace."}
                            )
            profile = attrs.get("retrieval_profile")
            if profile and profile.project_id != project.id:
                raise serializers.ValidationError(
                    {"retrieval_profile": "Retrieval profile must belong to the same workspace."}
                )
        return attrs


class AgentDetailSerializer(AgentSerializer):
    """Read-only serializer that includes resolved names for display."""

    base_model_name = serializers.CharField(source="base_model.display_name", read_only=True)
    connection_name = serializers.CharField(source="base_model.connection.name", read_only=True)
    retrieval_profile_name = serializers.CharField(source="retrieval_profile.name", read_only=True)
    knowledge_base_names = serializers.SerializerMethodField()
    tool_names = serializers.SerializerMethodField()
    openwebui_live = serializers.SerializerMethodField()

    class Meta(AgentSerializer.Meta):
        fields = AgentSerializer.Meta.fields + [
            "base_model_name", "connection_name", "retrieval_profile_name",
            "knowledge_base_names", "tool_names", "openwebui_live",
        ]

    def get_knowledge_base_names(self, obj) -> list[str]:
        return list(obj.knowledge_bases.values_list("name", flat=True))

    def get_tool_names(self, obj) -> list[str]:
        return list(obj.tools.values_list("name", flat=True))

    def get_openwebui_live(self, obj):
        """Live Open WebUI model entry, fetched on demand (None if unsynced/down).

        Only populated when the context sets ``fetch_live`` (the detail
        endpoint) — the list stays a pure cache read.
        """
        if not self.context.get("fetch_live"):
            return None
        from model_registry.services import agent_live_openwebui
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        return agent_live_openwebui(obj, user)
