"""Publisher package — placeholder until Story 3.5 lands.

Story 1.4 introduced :class:`ExpressionPublisher` here as a placeholder
Protocol; Story 3.4 deleted it (along with its referenced placeholder
event types) as part of the four-topic event-schema rebuild. Story 3.5
will re-create this package's public surface as ``EventPublisher`` (a
typing.Protocol with four publish methods) plus
:class:`Ros2EventPublisher` and :class:`LogEventPublisher` adapters.

Until Story 3.5 lands, this module is intentionally empty — exposing
no public symbols. Importing the package does nothing useful.
"""

__all__: list[str] = []
