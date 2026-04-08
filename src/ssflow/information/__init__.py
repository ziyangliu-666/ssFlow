"""ssFlow information ecosystem layer.

Phase I — supporting types for the OASIS-based information layer:

  - `Publication`: a single piece of content broadcast in the simulation
    (news brief, research note, policy statement, social post, company
    announcement, or a synthetic price-update). Carries author, content_type,
    text, round_idx, references, and authority weight.

The runtime behavior (feed aggregation, social graph propagation) is provided
by OASIS itself; this subpackage just defines the canonical Python representation
that the engine, feed reader, and report renderer all share.
"""

from .publication import Publication

__all__ = ["Publication"]
