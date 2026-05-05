"""Test isolation for the logging tests.

Both stdlib :mod:`logging` and structlog hold *global* state — the root
logger's handler list, structlog's processor chain, the cached BoundLogger
factory. Without per-test cleanup, ordering bugs creep in: e.g. test A
attaches a handler that leaks into test B's tmp_path.

The ``_reset_logging_state`` fixture below is autouse, so every test in
this package gets a clean slate before AND after it runs (the post-yield
cleanup closes file handles deterministically — important on Windows
where unclosed handles can break ``tmp_path`` teardown).
"""

import logging as _stdlib_logging
from collections.abc import Iterator

import pytest
import structlog


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Iterator[None]:
    """Reset stdlib + structlog global state around every test in this package."""
    # Pre-test cleanup. Anything left from a previous run gets nuked here.
    _stdlib_logging.getLogger().handlers.clear()
    structlog.reset_defaults()
    yield
    # Post-test cleanup. Close handler file descriptors before clearing the
    # list so RotatingFileHandler doesn't leave open files on Windows.
    for h in list(_stdlib_logging.getLogger().handlers):
        h.close()
    _stdlib_logging.getLogger().handlers.clear()
    structlog.reset_defaults()
