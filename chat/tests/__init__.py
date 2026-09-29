"""Chat tests.

httpx logs every request at INFO, and these tests make a lot of them; the output
is unreadable otherwise.
"""
import logging

logging.getLogger("httpx").setLevel(logging.WARNING)
