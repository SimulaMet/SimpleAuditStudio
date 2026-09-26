import re

from django.core.exceptions import ValidationError
from rest_framework import serializers

# Hostname that is either a dotted domain, an IP literal, or a single-label
# Docker Compose / k8s service name (e.g. "mock-model", "postgres"). DRF's
# stock URLField rejects single-label hosts, which would make every internal
# self-hosted endpoint uncreatable — so we accept them here while still
# requiring a valid http(s) scheme and a syntactically sane host.
_HOSTNAME_RE = re.compile(
    r"^(?:"
    r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"  # single label (service name)
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*"  # optional further labels
    r"|"
    r"[0-9A-Fa-f:]+(?::[0-9A-Fa-f]*)?"  # bare IPv6 (brackets already stripped)
    r"|"
    r"\d{1,3}(?:\.\d{1,3}){3}"  # IPv4 literal
    r")$"
)


class EndpointURLField(serializers.Field):
    """Validate a model endpoint base_url.

    Accepts ``http``/``https`` URLs whose host is a dotted domain, an IP
    literal, or a single-label container service name. Rejects missing schemes
    and malformed hosts. The path (e.g. ``/v1``) is preserved verbatim.
    """

    def to_internal_value(self, data):
        if not isinstance(data, str) or not data.strip():
            self.fail("invalid")
        url = data.strip()
        m = re.match(r"^(https?)://([^/@?#]+)(/.*)?$", url)
        if not m:
            self.fail("invalid")
        _scheme, authority, _path = m.group(1), m.group(2), m.group(3)
        # Strip userinfo if present; then split host from port.
        hostport = authority.rsplit("@", 1)[-1]
        if hostport.startswith("["):
            # IPv6 literal: [addr]:port or [addr]
            close = hostport.find("]")
            if close == -1:
                self.fail("invalid")
            host = hostport[1:close]
            rest = hostport[close + 1:]
            if rest and not (rest.startswith(":") and rest[1:].isdigit()):
                self.fail("invalid")
        elif ":" in hostport:
            host, _, port = hostport.partition(":")
            if not port.isdigit():
                self.fail("invalid")
        else:
            host = hostport
        if not _HOSTNAME_RE.match(host):
            self.fail("invalid")
        return url

    def fail(self, key, **kwargs):
        raise ValidationError(self.error_messages.get(key, "Enter a valid URL."), code=key)


