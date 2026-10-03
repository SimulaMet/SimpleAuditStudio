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
            "agent",
            "agent_config_snapshot",
            "auditor_config_snapshot",
            "judge_version",
            "judge_config_snapshot",
            "generation_parameters_snapshot",
            "trace_config",
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
    target_model_id = serializers.IntegerField()
    # Optional: target an Agent (model + knowledge + tools + retrieval) instead
    # of a bare model. When set, target_model_id must match the agent's base model.
    agent_id = serializers.IntegerField(required=False)
    auditor_model_id = serializers.IntegerField()
    judge_model_id = serializers.IntegerField()
    # How it grades: a judge version, or a judge (its latest version). Neither
    # = SimpleAudit's default judge, as in the library.
    judge_version_id = serializers.IntegerField(required=False)
    judge_id = serializers.IntegerField(required=False)
    # Optional trace acquisition config (Promptfoo parity). Absent/empty = no
    # tracing. Shape: {"mode": "builtin"|"tempo", "base_url": ..., ...}.
    trace_config = serializers.DictField(required=False, default=dict)
