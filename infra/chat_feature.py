"""Core-side access to the optional chat module.

The chat app is optional (SIMPLEAUDIT_CHAT); the core must not import it at
module load, so every core touchpoint goes through this one guarded helper.
"""


def chat_enabled() -> bool:
    """Whether the chat module is on, without importing it at module load.

    Reads ``chat.config.ENABLED`` at call time (not import time) so runtime
    toggles — e.g. tests patching the flag — are honoured.
    """
    try:
        from chat import config
    except ImportError:
        return False
    return config.ENABLED
