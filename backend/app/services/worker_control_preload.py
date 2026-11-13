"""Connection-free control definitions shared by image and import consumers."""
from __future__ import annotations

import gc

from app.services import resource_aware_worker, tasks  # noqa: F401

# These imports define policy/state bridges, without starting monitors,
# registering callbacks or creating database/Redis/HTTP connections. Effective
# service configuration must already be present before settings is imported.
gc.collect()
gc.freeze()
