from django.test import SimpleTestCase

from infra.source_chips import annotate_source_chips, source_chip


class SourceChipTests(SimpleTestCase):
    def test_deep_url_uses_last_segment_and_anchor(self):
        c = source_chip("https://legehandboka.no/handboken/psykiatri/selvmord-og-selvmordsforsok/#tiltak-og-rad")
        self.assertEqual(c["label"], "Selvmord og selvmordsforsok")
        self.assertEqual(c["sublabel"], "Tiltak og rad")
        self.assertEqual(c["host"], "legehandboka.no")
        self.assertTrue(c["href"].startswith("https://"))

    def test_domain_only(self):
        c = source_chip("https://nhi.no/")
        self.assertEqual(c["label"], "nhi.no")
        self.assertEqual(c["href"], "https://nhi.no/")

    def test_query_id(self):
        c = source_chip("https://example.org/view.aspx?p=Refund_Policy")
        self.assertEqual(c["label"], "Refund Policy")

    def test_opaque_segment_keeps_parent(self):
        c = source_chip("https://example.org/articles/3f9a2b7c1d4e5f60")
        self.assertTrue(c["label"].startswith("Articles · 3f9a2b7c"))

    def test_generic_filename_uses_parent_and_ext_badge(self):
        c = source_chip("https://example.org/guides/refunds/index.html")
        self.assertEqual((c["label"], c["ext"]), ("Refunds", "HTML"))

    def test_non_http_scheme_is_not_linked(self):
        c = source_chip("s3://bucket/policies/refund-policy.pdf")
        self.assertEqual((c["label"], c["ext"], c["href"], c["host"]), ("Refund policy", "PDF", None, "s3"))

    def test_unsafe_scheme_never_linked(self):
        self.assertIsNone(source_chip("javascript:alert(1)")["href"])
        self.assertIsNone(source_chip("data:text/html,<b>x</b>")["href"])

    def test_doi_links_resolver(self):
        c = source_chip("10.1000/xyz123")
        self.assertEqual(c["href"], "https://doi.org/10.1000/xyz123")

    def test_bare_domain_path_is_linked(self):
        self.assertEqual(source_chip("nhi.no/sykdommer/hodepine")["href"], "https://nhi.no/sykdommer/hodepine")

    def test_plain_title_and_file(self):
        self.assertEqual(source_chip("Acme Retail Policy")["label"], "Acme Retail Policy")
        self.assertIsNone(source_chip("Acme Retail Policy")["href"])
        c = source_chip("refunds.md")
        self.assertEqual((c["label"], c["ext"], c["kind"]), ("Refunds", "MD", "file"))

    def test_redacted_url_not_linked(self):
        c = source_chip("https://a.no/x/#klini[REDACTED]")
        self.assertTrue(c["redacted"])
        self.assertIsNone(c["href"])

    def test_document_object_title_and_score(self):
        c = source_chip({"source": "https://a.no/selvmord/", "title": "Selvmord: Generelt", "score": 0.770743, "id": "d1"})
        self.assertEqual((c["label"], c["score"], c["doc_id"]), ("Selvmord: Generelt", 0.77, "d1"))
        self.assertEqual(c["href"], "https://a.no/selvmord/")

    def test_empty(self):
        self.assertIsNone(source_chip(""))
        self.assertIsNone(source_chip(None))

    def test_annotate_marks_repeats_and_hidden(self):
        rows = [
            {"label": "RETRIEVAL-1", "kb_sources": ["https://a.no/x", "https://a.no/y", "https://a.no/y/", "b", "c"]},
            {"label": "RETRIEVAL-2", "kb_docs": [{"source": "https://a.no/x", "title": "X", "score": 0.5}]},
            {"label": "LLM-1"},
        ]
        annotate_source_chips(rows)
        self.assertEqual(len(rows[0]["kb_chips"]), 4)
        self.assertEqual(rows[0]["kb_hidden"], 1)
        chip = rows[1]["kb_chips"][0]
        self.assertEqual(chip["seen_in"], "RETRIEVAL-1")
        self.assertIn("Score: 0.5", chip["tooltip"])
        self.assertIn("Also returned by RETRIEVAL-1", chip["tooltip"])
        self.assertEqual(rows[2]["kb_chips"], [])
