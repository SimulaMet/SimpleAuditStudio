from rest_framework import serializers

from scenarios.models import Scenario, ScenarioRevision, ScenarioSet, ScenarioSetVersion

SEVERITY_CHOICES = ["pass", "low", "medium", "high", "critical"]


def _validate_severity_ceiling(value: str) -> str:
    value = (value or "").strip().lower()
    if value and value not in SEVERITY_CHOICES:
        raise serializers.ValidationError(f"Severity ceiling must be one of {SEVERITY_CHOICES}.")
    return value


class ScenarioRevisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScenarioRevision
        fields = ("id", "revision", "description", "expected_behavior", "test_prompt",
                  "severity_ceiling", "documents", "file_uri", "metadata", "content_hash", "created_at")
        read_only_fields = fields


class ScenarioListSerializer(serializers.ModelSerializer):
    latest_revision = serializers.SerializerMethodField()

    class Meta:
        model = Scenario
        fields = ("id", "key", "title", "category", "tags", "archived_at", "latest_revision", "created_at", "updated_at")
        read_only_fields = fields

    def get_latest_revision(self, obj) -> dict | None:
        revision = obj.revisions.order_by("-revision").first()
        if not revision:
            return None
        return ScenarioRevisionSerializer(revision).data


class ScenarioCreateSerializer(serializers.Serializer):
    key = serializers.CharField(required=False, allow_blank=True, default="")
    title = serializers.CharField(max_length=300)
    description = serializers.CharField(allow_blank=False)
    expected_behavior = serializers.ListField(child=serializers.CharField(), allow_empty=False)
    test_prompt = serializers.CharField(required=False, allow_blank=True, default="")
    severity_ceiling = serializers.CharField(required=False, allow_blank=True, default="", validators=[_validate_severity_ceiling])
    documents = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    file_uri = serializers.JSONField(required=False, allow_null=True, default=None)
    metadata = serializers.DictField(required=False, default=dict)
    category = serializers.CharField(required=False, allow_blank=True, default="")
    tags = serializers.ListField(child=serializers.CharField(), required=False, default=list)


class ScenarioUpdateSerializer(serializers.Serializer):
    description = serializers.CharField(allow_blank=False)
    expected_behavior = serializers.ListField(child=serializers.CharField(), allow_empty=False)
    test_prompt = serializers.CharField(required=False, allow_blank=True, default="")
    severity_ceiling = serializers.CharField(required=False, allow_blank=True, default="", validators=[_validate_severity_ceiling])
    documents = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    file_uri = serializers.JSONField(required=False, allow_null=True, default=None)
    metadata = serializers.DictField(required=False, default=dict)


class ScenarioSetSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScenarioSet
        fields = ("id", "name", "description", "created_at", "updated_at")
        read_only_fields = fields


class ScenarioSetCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=250)
    description = serializers.CharField(required=False, allow_blank=True, default="")


class PublishScenarioSetVersionSerializer(serializers.Serializer):
    scenario_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)


class ScenarioSetVersionItemSerializer(serializers.Serializer):
    position = serializers.IntegerField()
    scenario_id = serializers.IntegerField(source="scenario.id")
    scenario_key = serializers.CharField(source="scenario.key")
    scenario_title = serializers.CharField(source="scenario.title")
    revision_id = serializers.IntegerField(source="revision.id")
    revision = serializers.IntegerField(source="revision.revision")
    content_hash = serializers.CharField(source="revision.content_hash")


class ScenarioSetVersionSerializer(serializers.ModelSerializer):
    items = ScenarioSetVersionItemSerializer(many=True, source="items.all")

    class Meta:
        model = ScenarioSetVersion
        fields = ("id", "version", "scenario_count", "content_hash", "published_at", "items")
        read_only_fields = fields
