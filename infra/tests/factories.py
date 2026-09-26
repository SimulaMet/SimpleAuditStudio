"""Factory Boy definitions for all domain models.

Usage in any test:
    from infra.tests.factories import AuditRunFactory, ScenarioResultFactory
    run = AuditRunFactory()  # creates a full valid run with all FKs
"""
import factory
from factory.django import DjangoModelFactory

from accounts.models import Project, ProjectMembership, User
from audits.events import ScenarioResult
from audits.models import AuditRun
from model_registry.models import ModelConnection, RegisteredModel
from scenarios.models import (
    Scenario,
    ScenarioRevision,
    ScenarioSet,
    ScenarioSetVersion,
    ScenarioSetVersionItem,
)


class UserFactory(DjangoModelFactory):
    class Meta:
        model = User
    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda o: f"{o.username}@test.com")

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        user, _ = model_class.objects.get_or_create(
            username=kwargs.pop("username"), defaults=kwargs
        )
        return user


class ProjectFactory(DjangoModelFactory):
    class Meta:
        model = Project
    name = factory.Sequence(lambda n: f"Project {n}")
    slug = factory.Sequence(lambda n: f"project-{n}")


class MembershipFactory(DjangoModelFactory):
    class Meta:
        model = ProjectMembership
    user = factory.SubFactory(UserFactory)
    project = factory.SubFactory(ProjectFactory)
    role = "owner"


class ScenarioFactory(DjangoModelFactory):
    class Meta:
        model = Scenario
    project = factory.SubFactory(ProjectFactory)
    key = factory.Sequence(lambda n: f"scenario-{n:04d}")
    title = factory.Sequence(lambda n: f"Scenario {n}")
    category = "test"


class ScenarioRevisionFactory(DjangoModelFactory):
    class Meta:
        model = ScenarioRevision
    scenario = factory.SubFactory(ScenarioFactory)
    revision = 1
    description = "Test scenario description"
    expected_behavior = [{"criterion": "be helpful", "severity_if_violated": "medium"}]
    content_hash = factory.Faker("sha256")


class ScenarioSetFactory(DjangoModelFactory):
    class Meta:
        model = ScenarioSet
    project = factory.SubFactory(ProjectFactory)
    name = factory.Sequence(lambda n: f"Set {n}")


class ScenarioSetVersionFactory(DjangoModelFactory):
    class Meta:
        model = ScenarioSetVersion
    scenario_set = factory.SubFactory(ScenarioSetFactory)
    version = 1
    scenario_count = 1
    content_hash = factory.Faker("sha256")


class ScenarioSetVersionItemFactory(DjangoModelFactory):
    class Meta:
        model = ScenarioSetVersionItem
    version = factory.SubFactory(ScenarioSetVersionFactory)
    scenario = factory.SubFactory(ScenarioFactory)
    revision = factory.SubFactory(ScenarioRevisionFactory)
    position = factory.Sequence(lambda n: n + 1)


class ModelConnectionFactory(DjangoModelFactory):
    class Meta:
        model = ModelConnection
    project = factory.SubFactory(ProjectFactory)
    name = factory.Sequence(lambda n: f"Connection {n}")
    provider = "openai"
    base_url = "http://localhost:9999/v1"
    enabled = True


class RegisteredModelFactory(DjangoModelFactory):
    class Meta:
        model = RegisteredModel
    connection = factory.SubFactory(ModelConnectionFactory)
    project = factory.LazyAttribute(lambda o: o.connection.project)
    display_name = factory.Sequence(lambda n: f"Model {n}")
    model_id = factory.Sequence(lambda n: f"model-{n}")
    enabled = True




class AuditRunFactory(DjangoModelFactory):
    class Meta:
        model = AuditRun
    project = factory.SubFactory(ProjectFactory)
    name = factory.Sequence(lambda n: f"Audit Run {n}")
    status = AuditRun.Status.COMPLETED
    scenario_set_version = factory.SubFactory(ScenarioSetVersionFactory)
    target_model = factory.SubFactory(RegisteredModelFactory)
    auditor_model = factory.SubFactory(RegisteredModelFactory)
    judge_model = factory.SubFactory(RegisteredModelFactory)
    target_config_snapshot = {"model": "test", "params": {}}
    auditor_config_snapshot = {"model": "test", "params": {}}
    judge_config_snapshot = {"model": "test", "params": {}}
    generation_parameters_snapshot = {"temperature": 0.7, "max_tokens": 2048}
    simpleaudit_version = "0.1.0"
    git_commit = "abc1234"
    total_scenarios = 1
    completed_scenarios = 1


class ScenarioResultFactory(DjangoModelFactory):
    class Meta:
        model = ScenarioResult
    # run_id is a plain PositiveBigIntegerField (not a Django FK)
    run_id = 1
    version_item_id = factory.Sequence(lambda n: str(n + 100))
    status = "completed"
    result = {
        "severity": "pass",
        "conversation": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ],
        "issues_found": [],
        "rationale": "No issues found.",
    }


class RepeatedScenarioResultFactory(ScenarioResultFactory):
    """A scenario result with n_repetitions > 1."""
    result = {
        "reps": [
            {"severity": "high", "conversation": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "bad answer"}], "issues_found": ["hallucination"]},
            {"severity": "pass", "conversation": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "good answer"}], "issues_found": []},
            {"severity": "pass", "conversation": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "also good"}], "issues_found": []},
        ],
        "aggregated_severity": "pass",
        "agreement_rate": 0.6667,
        "severity_distribution": {"high": 1, "pass": 2},
        "n_repetitions": 3,
    }
