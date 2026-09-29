from django.apps import AppConfig


class ChatConfig(AppConfig):
    """The optional Open WebUI module (see chat/config.py)."""

    name = "chat"
    verbose_name = "Chat (Open WebUI)"

    def ready(self):
        """Follow model-connection changes, but only when chat is switched on."""
        from chat import config

        if config.ENABLED:
            from chat import signals  # noqa: F401  (registers the receivers)
