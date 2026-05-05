"""BeliefStateClient Protocol — fresh-on-every-turn belief-state read seam.

The pipeline asks the belief-state service for a focused subset of context
**at the start of each turn**, rather than holding cached state. This keeps
the pipeline stateless across turns and lets the belief service evolve
independently. v1 impl is :class:`HttpBeliefStateClient` (Story 4.1).
"""

from typing import Any, Protocol


class BeliefStateClient(Protocol):
    """Per-turn fresh belief-state read. v1 impl is HttpBeliefStateClient (Story 4.1)."""

    async def read(self, keys: list[str]) -> dict[str, Any]:
        """Fetch a focused subset of belief-state values by key.

        Args:
            keys: Keys to retrieve. The set of allowed keys is configured
                in ``setup.toml`` (``[talker].grounded_keys``) — Story 4.1
                wires the validation. Unknown keys may either return
                ``None`` or raise, depending on the impl.

        Returns:
            Mapping of key → value. Values are JSON-deserializable (the
            belief service returns JSON), so :data:`Any` is the honest
            type — pinning shapes per key would need a schema overhaul.
        """
        ...
