from django.apps import AppConfig


def _chat_preference_keys() -> set:
    """This app's preference keys, present only while chat is on."""
    from chat import config

    return {"chat_model"} if config.ENABLED else set()


class ChatConfig(AppConfig):
    """The optional Open WebUI module (see chat/config.py)."""

    name = "chat"
    verbose_name = "Chat (Open WebUI)"

    def ready(self):
        """Follow model-connection changes, but only when chat is switched on."""
        from chat import config
        from infra import runs_table

        # Contribute this app's preference key to the core's allow-list. The
        # core checks it at request time, so the key is only accepted while
        # chat is on (and rejected once it is switched off).
        runs_table.EXTRA_PREFERENCE_KEY_PROVIDERS.append(_chat_preference_keys)

        if config.ENABLED:
            from chat import signals  # noqa: F401  (registers the receivers)
