"""Public marketing pages + technical SEO endpoints (sitemap, robots, llms.txt).

These are the only indexable surface of the site: everything else is behind a
login wall and served with ``noindex``. The pages are server-rendered, require
no authentication, and carry full 2025/2026-standard SEO metadata (unique
titles, meta descriptions, canonicals, Open Graph/Twitter cards, and JSON-LD
structured data) via the shared ``templates/seo/head.html`` partial.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from django.http import HttpResponse
from django.views.generic import TemplateView

# ─── Site identity (single source of truth for schema + copy) ────────────────

SITE_NAME = "SimpleAudit Studio"
SITE_TAGLINE = "Reproducible AI Model Auditing Platform"
GITHUB_URL = "https://github.com/SushantGautam/SimpleAuditStudio"
PYPI_URL = "https://pypi.org/project/simpleaudit-studio/"
ENGINE_URL = "https://github.com/kelkalot/simpleaudit"
LICENSE = "MIT"


def _absolute_url(request, path: str) -> str:
    return request.build_absolute_uri(path)


def _base_context(request) -> dict:
    return {
        "current_year": datetime.now(UTC).year,
        "og_image_url": _absolute_url(request, "/static/logo.png"),
    }


# ─── Structured data builders ────────────────────────────────────────────────

def _organization_schema(request) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": SITE_NAME,
        "url": _absolute_url(request, "/"),
        "logo": _absolute_url(request, "/static/logo.png"),
        "description": (
            "A self-hostable platform for running reproducible audits of AI models, "
            "wrapping the SimpleAudit Target → Auditor → Judge engine."
        ),
        "sameAs": [GITHUB_URL, PYPI_URL],
        "license": LICENSE,
    }


def _website_schema(request) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": SITE_NAME,
        "url": _absolute_url(request, "/"),
        "description": SITE_TAGLINE,
    }


def _software_schema(request) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": SITE_NAME,
        "operatingSystem": "Any (web-based; Docker Compose or uvx)",
        "applicationCategory": "DeveloperApplication",
        "url": _absolute_url(request, "/"),
        "description": (
            "Self-hostable platform for reproducible AI model auditing. Freeze inputs, "
            "run Target → Auditor → Judge pipelines, and compare runs over time."
        ),
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
        "license": LICENSE,
        "author": {"@type": "Organization", "name": SITE_NAME, "url": GITHUB_URL},
    }


# ─── Public page views ───────────────────────────────────────────────────────

class LandingView(TemplateView):
    template_name = "public/landing.html"

    def get_context_data(self, **kw):
        ctx = _base_context(self.request)
        ctx.update(
            page_title=f"{SITE_NAME} — {SITE_TAGLINE}",
            page_description=(
                "SimpleAudit Studio is a self-hostable platform for running reproducible AI model "
                "audits. Freeze inputs, compare runs, and judge LLM behavior with the SimpleAudit "
                "Target → Auditor → Judge engine."
            ),
            page_robots="index,follow",
            page_jsonld=[
                json.dumps(_organization_schema(self.request)),
                json.dumps(_website_schema(self.request)),
                json.dumps(_software_schema(self.request)),
            ],
        )
        return super().get_context_data(**ctx)


# ─── Technical SEO endpoints ─────────────────────────────────────────────────

# Only these routes are indexable; everything else is app UI behind login.
# Paths are absolute (leading slash) so build_absolute_uri resolves them
# correctly regardless of the current request path.
_INDEXABLE_ROUTES = [
    ("/", "landing"),
]


def sitemap_xml(request):
    """XML sitemap listing only the public, indexable pages."""
    now = datetime.now(UTC).strftime("%Y-%m-%d")
    urls = []
    for path, _name in _INDEXABLE_ROUTES:
        loc = _absolute_url(request, path)
        priority = "1.0" if path == "/" else "0.8"
        urls.append(
            f"  <url>\n    <loc>{loc}</loc>\n    "
            f"<lastmod>{now}</lastmod>\n    <changefreq>weekly</changefreq>\n"
            f"    <priority>{priority}</priority>\n  </url>"
        )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )
    return HttpResponse(body, content_type="application/xml")


def robots_txt(request):
    """robots.txt: allow public pages, block app/API/admin, reference sitemap."""
    lines = [
        "# SimpleAudit Studio robots.txt",
        "# Public marketing pages are indexable; the application itself is",
        "# behind a login wall and intentionally excluded from search indexes.",
        "",
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin/",
        "Disallow: /api/",
        "Disallow: /login/",
        "Disallow: /register/",
        "Disallow: /logout/",
        "Disallow: /dashboard/",
        "Disallow: /workspaces/",
        "Disallow: /health/",
        "Disallow: /audits/",
        "Disallow: /scenarios/",
        "Disallow: /models/",
        "Disallow: /compare/",
        "Disallow: /auth/",
        "Disallow: /static/",
        "",
        f"Sitemap: {_absolute_url(request, '/sitemap.xml')}",
    ]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain")


def llms_txt(request):
    """llms.txt — plain-text description for LLM/AI crawlers (2025+ convention)."""
    base = _absolute_url(request, "/").rstrip("/")
    lines = [
        f"# {SITE_NAME}",
        "",
        (
            f"> {SITE_TAGLINE}. A self-hostable platform for running reproducible audits of AI models, "
            "wrapping the open-source SimpleAudit Target → Auditor → Judge engine."
        ),
        "",
        "## What it does",
        "- Design and version test scenarios that probe model behavior",
        "- Register any OpenAI-compatible model endpoint (Ollama, vLLM, OpenAI, Together, Groq)",
        "- Run durable, observable audits that survive restarts",
        "- Compare completed runs across common scenarios",
        "- Freeze every run's inputs (scenarios, config, judge, engine version) for reproducibility",
        "",
        "## Key links",
        f"- Home: {base}/",
        f"- GitHub: {GITHUB_URL}",
        f"- PyPI: {PYPI_URL}",
        f"- SimpleAudit engine: {ENGINE_URL}",
        "",
        "## License",
        f"{LICENSE} — see {GITHUB_URL}",
    ]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain")
