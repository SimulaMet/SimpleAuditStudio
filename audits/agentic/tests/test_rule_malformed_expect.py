"""A rule with a malformed ``expect`` is flagged, never crashes its description."""
from django.test import SimpleTestCase

from audits.agentic.rule_engine import check_code, describe_check


class MalformedExpectTest(SimpleTestCase):
    def test_malformed_expect_is_flagged_not_raised(self):
        base = {"check": "count", "select": {"kind": "tool", "where": []}}
        for expect in ({"op": "between", "value": [1]}, {"op": "bogus", "value": 1}, None,
                       {"op": "between", "value": "1,3"}):
            rule = dict(base) if expect is None else {**base, "expect": expect}
            self.assertIn("malformed", describe_check(rule))
            self.assertIn("malformed", check_code(rule))

    def test_valid_rule_still_described(self):
        rule = {"check": "count", "select": {"kind": "tool", "where": []}, "expect": {"op": "at_least", "value": 2}}
        self.assertEqual(describe_check(rule), "At least 2 tool call")

    def test_none_tool_rule_with_non_equals_argument(self):
        rule = {"check": "none", "select": {"kind": "tool", "where": [
            {"field": "tool.name", "op": "equals", "value": "issue_refund"},
            {"field": "tool.argument.amount", "op": "at_least", "value": 100},
        ]}}
        self.assertTrue(describe_check(rule).startswith("No tool call where"))
        check_code(rule)
