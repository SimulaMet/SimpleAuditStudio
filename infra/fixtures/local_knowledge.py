"""Studio-only stand-in for knowledge search in direct (non-Open WebUI) agent runs.

When Studio calls an agent's base model directly, Open WebUI's built-in knowledge
tools are not there, so this keyword search over the repo's policy fixtures takes
their place. It is deliberately NOT part of the tool pushed to Open WebUI: inside
Open WebUI the agent uses Open WebUI's own knowledge search.
"""
import os

from infra.fixtures.sample_tools.order_lookup import Tools


def _docs_dir() -> str:
    """The policy documents in the repo fixtures."""
    return os.path.join(os.path.dirname(__file__), "sample_docs")


class LocalTools(Tools):
    """The Acme order tools plus the local knowledge search."""

    def acme_search_knowledge(self, query: str, top_k: int = 3):
        """Search the Acme Retail policy knowledge base for relevant passages.

        Performs a keyword-based retrieval over the policy documents
        (refunds, returns, shipping, eligibility exceptions, tracking).
        Returns the most relevant passages with their source document name.

        Args:
            query (str): Natural-language search query, e.g. 'electronics
                return window' or 'defective item exception'.
            top_k (int): Maximum number of passages to return (default 3).

        Returns a list of {source, passage} dicts ranked by relevance.
        """
        import os
        import re

        docs_dir = os.path.normpath(_docs_dir())
        if not os.path.isdir(docs_dir):
            return [{"source": "error", "passage": f"Docs directory not found: {docs_dir}"}]

        # Tokenize query into keywords (lowercase, alphanumeric words > 2 chars).
        tokens = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
        if not tokens:
            return [{"source": "no_query", "passage": "Empty query."}]

        scored: list[tuple[int, str, str]] = []
        for fname in sorted(os.listdir(docs_dir)):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(docs_dir, fname)
            try:
                with open(fpath, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                continue
            # Split into paragraphs as candidate passages.
            paragraphs = [p.strip() for p in re.split(r"\n\n+", text) if len(p.strip()) > 40]
            for para in paragraphs:
                para_lower = para.lower()
                score = sum(1 for tok in tokens if tok in para_lower)
                if score > 0:
                    scored.append((score, fname, para[:500]))

        scored.sort(key=lambda x: -x[0])
        results = [
            {"source": src, "passage": passage}
            for _, src, passage in scored[:top_k]
        ]
        if not results:
            return [{"source": "no_match", "passage": f"No passages matched query: {query!r}"}]
        return results
