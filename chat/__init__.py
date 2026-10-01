"""Open WebUI, embedded in Studio and signed in with Studio's identity.

An optional module: with SIMPLEAUDIT_CHAT unset or disabled, its URLs 404, the
sidebar has no Chat entry and nothing extra runs. See docs/chat.md.
"""
default_app_config = "chat.apps.ChatConfig"
