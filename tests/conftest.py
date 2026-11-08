"""Tests never call a real model or Gmail; the runtime only needs a key to be present."""

import os

os.environ.setdefault("OPENROUTER_API_KEY", "test-key-not-used")
