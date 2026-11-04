"""Knowledge search is Studio's local stand-in, not part of the Open WebUI tool."""
from pathlib import Path

from infra.fixtures import local_knowledge
from infra.fixtures.local_knowledge import LocalTools
from infra.fixtures.sample_tools.order_lookup import Tools

_FIXTURE_DOCS = Path(__file__).resolve().parents[1] / "fixtures" / "sample_docs"


def test_open_webui_tool_has_no_knowledge_search():
    """Inside Open WebUI the agent uses Open WebUI's own knowledge search."""
    assert not hasattr(Tools, "acme_search_knowledge")


def test_local_search_reads_the_repo_fixtures():
    assert Path(local_knowledge._docs_dir()).resolve() == _FIXTURE_DOCS
    results = LocalTools().acme_search_knowledge("refund window for returns")
    assert results, "expected at least one passage"
    assert all(r["source"].endswith(".md") for r in results), results


def test_local_tools_keep_the_order_tools():
    assert LocalTools().acme_get_order("ACME-1001")["order_id"] == "ACME-1001"


def test_missing_documents_are_reported_not_raised(monkeypatch):
    monkeypatch.setattr(local_knowledge, "_docs_dir", lambda: "/nonexistent/acme/docs")
    results = LocalTools().acme_search_knowledge("refund")
    assert results[0]["source"] == "error"
    assert "Docs directory not found" in results[0]["passage"]
