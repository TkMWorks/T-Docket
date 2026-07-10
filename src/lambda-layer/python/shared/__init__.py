"""
Shared helpers for the document storage API's Lambda handlers.

Packaged as a Lambda layer (contents under a `python/` prefix) and
attached to every API-layer function -- presign_upload, list_documents,
get_document, update_annotation -- so each function's own deployment
package only needs to contain its own handler.py.

This file re-exports the small set of names handlers actually use, so
handler code can do:

    from shared import AuthError, get_user_sub, response

instead of importing shared.auth and shared.responses separately. If
you add more helpers to this package later, decide whether they belong
in __all__ -- anything not listed here is still importable directly
from its submodule, but won't show up in `from shared import *` or
autocomplete, which keeps the layer's public surface intentional as it
grows.
"""

from .auth import AuthError, get_user_sub
from .responses import response

__all__ = ["AuthError", "get_user_sub", "response"]

__version__ = "1.0.0"