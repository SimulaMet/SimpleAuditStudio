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


class AuditRunListSerializer(serializers.ModelSerializer):
    """A run as a list row: identity, progress, outcome; no config snapshots.

    The detail endpoint (and ``?expand=snapshots`` on the list) carries the
    frozen target/auditor/judge/agent/generation snapshots.
    """

    scenario_set_version_id = serializers.IntegerField(source="scenario_set_version.id", read_only=True)
    scenario_set_version_number = serializers.IntegerField(source="scenario_set_version.version", read_only=True)
    scenario_set_version_hash = serializers.CharField(source="scenario_set_version.content_hash", read_only=True)
    target_model = serializers.SerializerMethodField()
    judge_model = serializers.SerializerMethodField()
    judge = serializers.SerializerMethodField()
    pass_rate = serializers.SerializerMethodField()

    class Meta:
        model = AuditRun
        fields = (
            "id",
            "name",
            "status",
            "scenario_set_version_id",
            "scenario_set_version_number",
            "scenario_set_version_hash",
            "target_model",
            "agent",
            "judge_model",
            "judge_version",
            "judge",
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
            "pass_rate",
            "error_code",
            "error_message",
            "created_at",
        )
        read_only_fields = fields

    def get_target_model(self, run) -> str:
        from audits.services import frozen_name

        return frozen_name(run, "target")

    def get_judge_model(self, run) -> str:
        from audits.services import frozen_name

        return frozen_name(run, "judge")

    def get_judge(self, run) -> str:
        from audits.services import frozen_judge

        return frozen_judge(run)["label"]

    def get_pass_rate(self, run) -> float | None:
        """Passing judged trials, percent (``None`` before any trial is judged)."""
        c = (self.context.get("pass_counts") or {}).get(run.id) or {}
        return round(c["k"] * 100 / c["n"], 1) if c.get("n") else None


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
    # Optional generation settings (mirrors the web form's managed fields).
    max_turns = serializers.IntegerField(required=False, min_value=1, max_value=20)
    n_repetitions = serializers.IntegerField(required=False, min_value=1, max_value=10)
    language = serializers.CharField(required=False, allow_blank=True, max_length=60)
