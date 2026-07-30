"""Offline-injectable M4 Web search and bounded browser backend seams.

Import ``search`` and ``browser`` explicitly.  Keeping this package initializer
side-effect free prevents the provider-neutral HTTPS search seam from importing
Discord or the browser worker when only search is used.

Nothing here is connected to the runtime composition root by default.
"""
