"""promptpin — git-native prompt registry.

Prompts live as files in your repo (``prompts/``). Versions are git commits,
human names are namespaced tags (``promptpin/<name>@v3``), and deployments are
pins recorded in a committed ``promptpin.lock`` file. Local-first: no server,
no vendor database, nothing on the request hot path.
"""

__version__ = "0.3.0"

from promptpin.client import (
    load_bundle,
    log_serve,
    render,
    render_messages,
    render_template,
    resolve,
)
from promptpin.runtime import PinnedPrompts, PromptIntegrityError, Receipt

__all__ = [
    "PinnedPrompts",
    "PromptIntegrityError",
    "Receipt",
    "__version__",
    "load_bundle",
    "log_serve",
    "render",
    "render_messages",
    "render_template",
    "resolve",
]
