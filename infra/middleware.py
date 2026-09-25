"""Request-scoped observability middleware.

Injects a unique request ID into every response header and attaches
correlation fields (request_id, user_id) to all log records emitted
during the request lifecycle. This enables tracing a single API call
across web + worker logs without OpenTelemetry.
"""
import logging
import uuid

from django.utils.deprecation import MiddlewareMixin


class RequestIDMiddleware(MiddlewareMixin):
    """Assigns a UUID to each request and exposes it as X-Request-ID.

    Also sets the thread-local correlation context so all log records
    emitted during this request carry the request_id (and user_id once
    authenticated).
    """

    def process_request(self, request):
        # Honour an upstream-provided ID (e.g. from a reverse proxy) or generate one.
        request.request_id = request.META.get("HTTP_X_REQUEST_ID") or uuid.uuid4().hex[:12]
        set_correlation_context(request_id=request.request_id)

    def process_response(self, request, response):
        if hasattr(request, "request_id"):
            response["X-Request-ID"] = request.request_id
        clear_correlation_context()
        return response


class CorrelationLogFilter(logging.Filter):
    """Logging filter that copies correlation fields from the current request.

    Usage: add to any handler's filters list. The filter reads from a
    thread-local set by the middleware via ``set_correlation_context``.
    """

    _local = __import__("threading").local()

    @classmethod
    def set_context(cls, **fields):
        cls._local.fields = fields

    @classmethod
    def clear_context(cls):
        cls._local.fields = {}

    def filter(self, record):
        fields = getattr(self._local, "fields", {})
        for key, value in fields.items():
            setattr(record, key, value)
        return True


def set_correlation_context(**fields):
    """Set correlation fields for the current thread's log records."""
    CorrelationLogFilter.set_context(**fields)


def clear_correlation_context():
    """Clear correlation fields (call in finally/teardown)."""
    CorrelationLogFilter.clear_context()

class CsrfCookieMiddleware(MiddlewareMixin):
    """Guarantees the csrftoken cookie is set for authenticated users.

    Django only sets the cookie when a response renders ``{% csrf_token %}``.
    In the HF Space iframe (demo mode) the login page may be the only place
    that does so; if the user signed in via WorkOS magic auth or the cookie
    expired, subsequent fetch POSTs fail with 403.

    We only set the cookie if it is missing, rather than rotating it on every
    request. Rotating on every request causes token/cookie drift: a form
    rendered with token A becomes stale after any other request rotates the
    cookie to secret B, producing 403 "CSRF token from POST incorrect".
    """

    def process_request(self, request):
        if hasattr(request, "user") and request.user.is_authenticated:
            from django.conf import settings
            from django.middleware.csrf import _add_new_csrf_cookie

            # Only set the cookie if it's not already present in the request.
            existing = request.COOKIES.get(settings.CSRF_COOKIE_NAME)
            if not existing:
                _add_new_csrf_cookie(request)


class ProjectMiddleware(MiddlewareMixin):
    """Attaches the user's active project to the request.

    Uses the session key ``active_project_id`` if set, otherwise falls back
    to the user's first project. This lets UI views access ``request.project``
    without requiring a URL parameter.
    """

    def process_request(self, request):
        request.project = None
        if not hasattr(request, "user") or not request.user.is_authenticated:
            return
        from accounts.models import Project

        project_id = request.session.get("active_project_id")
        if project_id:
            request.project = Project.objects.filter(pk=project_id).first()
        if not request.project:
            membership = request.user.memberships.select_related("project").first()
            if membership:
                request.project = membership.project
                request.session["active_project_id"] = request.project.id