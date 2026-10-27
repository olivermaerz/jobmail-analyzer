"""Mail ingest plugins.

Add a new source by implementing MailSource and registering it in registry.py.
Don't teach classify.py about Apple headers — that's the plugin's job.
"""

from jobmail.ingest.registry import get_source, list_sources

__all__ = ["get_source", "list_sources"]
