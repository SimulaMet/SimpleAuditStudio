"""The order lookup tool finds its policy documents without any configuration."""
import sys
import types
from pathlib import Path

from infra.fixtures.sample_tools import order_lookup
from infra.fixtures.sample_tools.order_lookup import Tools

_FIXTURE_DOCS = Path(__file__).resolve().parents[1] / "fixtures" / "sample_docs"


def test_search_finds_the_documents_outside_open_webui(monkeypatch):
    monkeypatch.setattr(order_lookup, "_docs_dir", lambda: str(_FIXTURE_DOCS))
    results = Tools().acme_search_knowledge("refund window for returns")
    assert results, "expected at least one passage"
    assert all(r["source"].endswith(".md") for r in results), results


def test_documents_sit_inside_the_open_webui_package(monkeypatch, tmp_path):
    fake = types.ModuleType("open_webui")
    fake.__file__ = str(tmp_path / "__init__.py")
    monkeypatch.setitem(sys.modules, "open_webui", fake)
    assert order_lookup._docs_dir() == str(tmp_path / "acme_sample_docs")


def test_missing_documents_are_reported_not_raised(monkeypatch):
    monkeypatch.setattr(order_lookup, "_docs_dir", lambda: "/nonexistent/acme/docs")
    results = Tools().acme_search_knowledge("refund")
    assert results[0]["source"] == "error"
    assert "Docs directory not found" in results[0]["passage"]
