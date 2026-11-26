"""Scenario drafts: edits saved without publishing, then published together as one set version."""
from django.test import Client, TestCase

from infra.tests.factories import MembershipFactory, ProjectFactory, UserFactory
from scenarios.models import ScenarioRevision, ScenarioSetVersionItem
from scenarios.services import (
    create_scenario,
    create_scenario_set,
    pending_drafts,
    publish_scenario_set_version,
)


class ScenarioDraftTestCase(TestCase):
    """A set "Triage" at v1 with three scenarios, each on revision 1."""

    def setUp(self):
        self.project = ProjectFactory()
        self.user = UserFactory()
        MembershipFactory(user=self.user, project=self.project)
        self.client = self._client(self.user)
        self.sset = create_scenario_set(project=self.project, user=self.user, name="Triage")
        self.s1, self.s2, self.s3 = (
            create_scenario(
                project=self.project, user=self.user, key=f"s{n}", title=f"Scenario {n}",
                description=f"Original {n}", expected_behavior=["Be safe"],
            )
            for n in (1, 2, 3)
        )
        publish_scenario_set_version(
            scenario_set=self.sset, user=self.user, scenario_ids=[self.s1.id, self.s2.id, self.s3.id],
        )

    def _client(self, user) -> Client:
        client = Client()
        client.force_login(user)
        session = client.session
        session["active_project_id"] = self.project.pk
        session.save()
        return client

    def _edit(self, scenario, description, *, draft=True, base=None, client=None, sset=None):
        """Post the scenario edit form: "Save draft" by default, "Save" with ``draft=False``."""
        data = {
            "set_id": (sset or self.sset).id, "title": scenario.title, "description": description,
            "expected_behavior": "Be safe",
        }
        if draft:
            data["publish"] = "0"
        if base is not None:
            data["base_revision_id"] = base
        return (client or self.client).post(f"/scenarios/edit/{scenario.id}/", data, follow=True)

    def _drafts(self, action="", base_version=None, client=None):
        data = {"action": action} if action else {}
        if base_version is not None:
            data["base_version"] = base_version
        return (client or self.client).post(f"/scenarios/drafts/{self.sset.id}/", data, follow=True)

    def _versions(self) -> int:
        return self.sset.versions.count()

    def _held(self, scenario, sset=None) -> int:
        """The revision number of ``scenario`` in the set's latest version."""
        latest = (sset or self.sset).versions.order_by("-version").first()
        return latest.items.get(scenario=scenario).revision.revision

    def _revisions(self, scenario) -> list[int]:
        return list(scenario.revisions.order_by("revision").values_list("revision", flat=True))

    def _pending(self) -> dict[int, int]:
        return {scenario_id: revision.revision for scenario_id, revision in pending_drafts(self.sset).items()}


class SaveDraftTests(ScenarioDraftTestCase):
    def test_draft_adds_a_revision_and_publishes_no_version(self):
        response = self._edit(self.s1, "Edited 1")

        self.assertContains(response, "saved as a draft (not published)")
        self.assertEqual(self._versions(), 1)
        self.assertEqual(self._revisions(self.s1), [1, 2])
        self.assertEqual(self._held(self.s1), 1)
        self.assertEqual(self._pending(), {self.s1.id: 2})

    def test_draft_without_a_content_change_is_no_draft(self):
        response = self._edit(self.s1, "Original 1")

        self.assertContains(response, "nothing to publish")
        self.assertEqual(self._revisions(self.s1), [1])
        self.assertEqual(self._pending(), {})

    def test_draft_edited_back_to_the_published_text_is_no_draft(self):
        self._edit(self.s1, "Edited 1")
        self._edit(self.s1, "Original 1", base=self.s1.revisions.get(revision=2).pk)

        self.assertEqual(self._revisions(self.s1), [1, 2, 3])
        self.assertEqual(self._pending(), {})

    def test_page_shows_a_badge_per_draft_and_the_banner(self):
        page = f"/scenarios/?set={self.sset.id}"
        self.assertNotContains(self.client.get(page), "unpublished edits")

        self._edit(self.s1, "Edited 1")
        self._edit(self.s2, "Edited 2")

        html = self.client.get(page).content.decode()
        self.assertIn("2 scenarios with unpublished edits", html)
        self.assertIn("Publish as v2", html)
        self.assertEqual(html.count(">draft, not published<"), 2)
        # The list still shows what runs execute: the published text.
        self.assertIn("Original 1", html)
        self.assertNotIn("Edited 1", html)

    def test_edit_form_opens_on_the_draft(self):
        self._edit(self.s1, "Edited 1")
        draft = self.s1.revisions.get(revision=2)
        items = {item.scenario_id: item for item in self.sset.versions.get(version=1).items.all()}

        form = self.client.get(f"/scenarios/edit-form/{items[self.s1.id].pk}/").content.decode()
        self.assertIn("Edited 1", form)
        self.assertIn(f'name="base_revision_id" value="{draft.pk}"', form)
        self.assertIn("Unpublished draft", form)

        untouched = self.client.get(f"/scenarios/edit-form/{items[self.s2.id].pk}/").content.decode()
        self.assertIn("Original 2", untouched)
        self.assertNotIn("Unpublished draft", untouched)

    def test_second_edit_continues_the_draft_and_a_stale_form_is_refused(self):
        self._edit(self.s1, "Edited 1")
        published, draft = self.s1.revisions.order_by("revision")

        stale = self._edit(self.s1, "From a form opened before the draft", base=published.pk)
        self.assertContains(stale, "changed since the form was opened")
        self.assertEqual(self._revisions(self.s1), [1, 2])

        self._edit(self.s1, "Edited twice", base=draft.pk)
        self.assertEqual(self._pending(), {self.s1.id: 3})
        self.assertEqual(self._versions(), 1)


class PublishDraftTests(ScenarioDraftTestCase):
    def test_drafts_of_several_scenarios_are_published_as_one_version(self):
        self._edit(self.s1, "Edited 1")
        self._edit(self.s2, "Edited 2")

        response = self._drafts(base_version=1)

        self.assertContains(response, "2 edited scenarios published as v2")
        self.assertEqual(self._versions(), 2)
        self.assertEqual([self._held(s) for s in (self.s1, self.s2, self.s3)], [2, 2, 1])
        self.assertEqual(self._pending(), {})
        self.assertNotContains(self.client.get(f"/scenarios/?set={self.sset.id}"), "unpublished edits")

    def test_save_with_no_further_change_publishes_the_draft(self):
        self._edit(self.s1, "Edited 1")
        self._edit(self.s2, "Edited 2")
        draft = self.s1.revisions.get(revision=2)

        response = self._edit(self.s1, "Edited 1", draft=False, base=draft.pk)

        self.assertContains(response, "new version published")
        self.assertEqual(self._versions(), 2)
        self.assertEqual(self._revisions(self.s1), [1, 2])
        self.assertEqual(self._held(self.s1), 2)
        # The other scenario's draft is still waiting.
        self.assertEqual(self._held(self.s2), 1)
        self.assertEqual(self._pending(), {self.s2.id: 2})

    def test_draft_survives_publishes_that_do_not_touch_it(self):
        self._edit(self.s1, "Edited 1")

        self.client.post(f"/scenarios/delete/{self.s3.id}/", {"set_id": self.sset.id})   # v2 without s3
        self._edit(self.s2, "Edited 2", draft=False)                                      # v3 with s2's edit

        self.assertEqual(self._versions(), 3)
        self.assertEqual((self._held(self.s1), self._held(self.s2)), (1, 2))
        self.assertEqual(self._pending(), {self.s1.id: 2})

    def test_reverted_set_has_no_drafts(self):
        self._edit(self.s1, "Edited 1", draft=False)                                      # v2 holds revision 2
        self.client.post(f"/scenarios/revert/{self.sset.id}/", {"target_version": 1})     # v3 holds revision 1 again

        self.assertEqual((self._versions(), self._held(self.s1)), (3, 1))
        self.assertEqual(self._pending(), {})
        self.assertNotContains(self.client.get(f"/scenarios/?set={self.sset.id}"), "unpublished edits")

    def test_stale_page_cannot_publish(self):
        self._edit(self.s1, "Edited 1")
        self.client.post(f"/scenarios/delete/{self.s3.id}/", {"set_id": self.sset.id})   # v2, by someone else

        stale = self._drafts(base_version=1)
        self.assertContains(stale, "This set changed since the page was opened")
        self.assertEqual(self._versions(), 2)

        self._drafts(base_version=2)
        self.assertEqual((self._versions(), self._held(self.s1)), (3, 2))

    def test_publish_with_no_drafts_publishes_nothing(self):
        self.assertContains(self._drafts(base_version=1), "No drafts to publish")
        self.assertEqual(self._versions(), 1)

    def test_draft_of_a_set_whose_scenarios_are_all_deleted_publishes_nothing(self):
        from django.utils import timezone

        self._edit(self.s1, "Edited 1")
        for scenario in (self.s1, self.s2, self.s3):   # deleted, but still in v1
            scenario.archived_at = timezone.now()
            scenario.save()

        self.assertContains(self._drafts(base_version=1), "Nothing was published")
        self.assertEqual(self._versions(), 1)


class DiscardDraftTests(ScenarioDraftTestCase):
    def test_discard_deletes_the_drafts_and_keeps_published_revisions(self):
        self._edit(self.s1, "Published edit", draft=False)        # s1 revision 2, in v2
        self._edit(self.s1, "Draft a")                            # revision 3
        self._edit(self.s1, "Draft b")                            # revision 4
        self._edit(self.s2, "Draft of 2")                         # s2 revision 2

        response = self._drafts("discard", base_version=2)

        self.assertContains(response, "2 drafts discarded")
        self.assertEqual(self._revisions(self.s1), [1, 2])
        self.assertEqual(self._revisions(self.s2), [1])
        self.assertEqual((self._versions(), self._pending()), (2, {}))
        # Revision numbers stay in step: the next edit is revision 3.
        self._edit(self.s1, "Edited after the discard")
        self.assertEqual(self._revisions(self.s1), [1, 2, 3])


class SharedScenarioDraftTests(ScenarioDraftTestCase):
    """Scenario 1 is also in a second set, as after an export and import."""

    def setUp(self):
        super().setUp()
        self.other = create_scenario_set(project=self.project, user=self.user, name="Other set")
        publish_scenario_set_version(scenario_set=self.other, user=self.user, scenario_ids=[self.s1.id])

    def _pending_in_other(self) -> dict[int, int]:
        return {scenario_id: revision.revision for scenario_id, revision in pending_drafts(self.other).items()}

    def test_edit_published_from_another_set_is_no_draft_here(self):
        self._edit(self.s1, "Edited from the other set", draft=False, sset=self.other)   # published there

        self.assertEqual((self._held(self.s1, self.other), self._held(self.s1)), (2, 1))
        self.assertEqual(self._pending(), {})
        self.assertNotContains(self.client.get(f"/scenarios/?set={self.sset.id}"), "unpublished edits")
        # This set's edit form still opens on the revision it runs.
        item = self.sset.versions.get(version=1).items.get(scenario=self.s1)
        form = self.client.get(f"/scenarios/edit-form/{item.pk}/").content.decode()
        self.assertIn("Original 1", form)
        self.assertIn(f'name="base_revision_id" value="{self.s1.revisions.get(revision=1).pk}"', form)
        self.assertNotIn("Unpublished draft", form)
        # Nothing here to discard, and the published revision is untouched.
        self.assertContains(self._drafts("discard", base_version=1), "0 drafts discarded")
        self.assertEqual(self._revisions(self.s1), [1, 2])
        self.assertTrue(ScenarioSetVersionItem.objects.filter(revision__scenario=self.s1, revision__revision=2).exists())

    def test_draft_of_a_shared_scenario_shows_in_each_set_until_one_publishes_it(self):
        self._edit(self.s1, "Edited 1")

        self.assertEqual(self._pending(), {self.s1.id: 2})
        self.assertEqual(self._pending_in_other(), {self.s1.id: 2})

        self._drafts(base_version=1)

        self.assertEqual((self._pending(), self._pending_in_other()), ({}, {}))
        self.assertEqual((self._held(self.s1), self._held(self.s1, self.other)), (2, 1))


class DraftPermissionTests(ScenarioDraftTestCase):
    def test_viewer_cannot_save_publish_or_discard_drafts(self):
        self._edit(self.s1, "Edited 1")
        viewer = UserFactory()
        MembershipFactory(user=viewer, project=self.project, role="viewer")
        reader = self._client(viewer)

        self._edit(self.s2, "Viewer edit", client=reader)
        self._drafts(base_version=1, client=reader)
        self._drafts("discard", base_version=1, client=reader)

        self.assertEqual(self._revisions(self.s2), [1])
        self.assertEqual(self._versions(), 1)
        self.assertEqual(self._pending(), {self.s1.id: 2})
        # A viewer still sees that edits are waiting.
        self.assertContains(reader.get(f"/scenarios/?set={self.sset.id}"), "1 scenario with unpublished edits")

    def test_drafts_of_another_workspace_cannot_be_published(self):
        self._edit(self.s1, "Edited 1")
        outsider = UserFactory()
        other_project = ProjectFactory()
        MembershipFactory(user=outsider, project=other_project)
        client = Client()
        client.force_login(outsider)
        session = client.session
        session["active_project_id"] = other_project.pk
        session.save()

        client.post(f"/scenarios/drafts/{self.sset.id}/", {"base_version": 1})

        self.assertEqual(self._versions(), 1)
        self.assertEqual(ScenarioRevision.objects.filter(scenario=self.s1).count(), 2)
