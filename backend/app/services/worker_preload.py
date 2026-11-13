"""Connection-free initialization shared by consumers in one Linux service."""
from __future__ import annotations

import gc
import os

import rq  # noqa: F401 - shared framework definitions, not Worker instances
import app.models  # noqa: F401
from sqlalchemy.orm import configure_mappers
from app.services import resource_descriptors  # noqa: F401

configure_mappers()
gc.collect()
gc.freeze()
# Children use this to distinguish an inherited preload from a late import.
PRELOAD_PID = os.getpid()
