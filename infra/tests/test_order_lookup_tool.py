"""The order lookup tool carries its policy documents, so it works wherever it runs."""
from pathlib import Path

from infra.fixtures.sample_tools import order_lookup
from infra.fixtures.sample_tools.order_lookup import Tools

_DOCS = Path(__file__).resolve().parents[1] / "fixtures" / "sample_docs"


def test_embedded_documents_match_the_fixture_files():
    on_disk = {p.name: p.read_text(encoding="utf-8") for p in sorted(_DOCS.glob("*.md"))}
    assert order_lookup._POLICY_DOCS == on_disk


def test_search_answers_without_any_docs_path(monkeypatch):
    monkeypatch.delenv("ACME_SAMPLE_DOCS_DIR", raising=False)
    results = Tools().acme_search_knowledge("refund window for returns")
    assert results, "expected at least one passage"
    assert all(r["source"].endswith(".md") for r in results), results
