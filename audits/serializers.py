from rest_framework import serializers

from audits.models import AuditRun


class AuditRunSerializer(serializers.ModelSerializer):
    scenario_set_version_id = serializers.IntegerField(source="scenario_set_version.id", read_only=True)
    scenario_set_version_number = serializers.IntegerField(source="scenario_set_version.version", read_only=True)
    scenario_set_version_hash = serializers.CharField(source="scenario_set_version.content_hash", read_only=True)

    class Meta:
        model = AuditRun
        fields = (
            "id",
            "name",
            "status",
            "scenario_set_version_id",
            "scenario_set_version_number",
            "scenario_set_version_hash",
            "target_config_snapshot",
            "auditor_config_snapshot",
            "judge_config_snapshot",
            "generation_parameters_snapshot",
            "simpleaudit_version",
            "git_commit",
            "queued_at",
            "started_at",
            "finished_at",
            "total_scenarios",
            "completed_scenarios",
            "successful_scenarios",
            "failed_scenarios",
            "retried_scenarios",
            "summary_metrics",
            "error_code",
            "error_message",
            "created_at",
        )
        read_only_fields = fields


class AuditRunCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=250)
    scenario_set_version_id = serializers.IntegerField()
    target_endpoint_id = serializers.IntegerField()
    auditor_endpoint_id = serializers.IntegerField()
    judge_endpoint_id = serializers.IntegerField()
