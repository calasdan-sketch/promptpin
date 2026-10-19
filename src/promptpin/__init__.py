"""promptpin — git-native prompt registry.

Prompts live as files in your repo (``prompts/``). Versions are git commits,
human names are namespaced tags (``promptpin/<name>@v3``), and deployments are
pins recorded in a committed ``promptpin.lock`` file. Local-first: no server,
no vendor database, nothing on the request hot path.
"""

__version__ = "0.1.1"

from promptpin.client import load_bundle, log_serve, render_template, resolve

__all__ = ["__version__", "load_bundle", "log_serve", "render_template", "resolve"]
