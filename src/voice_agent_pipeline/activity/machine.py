"""``ActivityFSM`` — the 7-state activity FSM (Story 4.3).

The FSM is the conversation-shape spine of Epic 4. It tracks where
OLAF is in the wake → speak → sleep loop, drives the mic-mode flip
(Story 4.6), schedules deferred-sleep on the ``go_to_sleep`` tool
call (Story 4.4 / FR46), and publishes :class:`ActivityEvent` on
every transition.

State set (architecture.md §"Activity FSM + Mood Control + Tool Registry"):

- ``starting`` — pre-init; transitions to ``sleeping`` on ``start()``.
- ``sleeping`` — wake-word listener engaged; ``vad_stt`` is suspended.
- ``waking`` — wake fired; greeting flows; mic flips to ``vad_stt``.
- ``listening`` — VAD + STT capturing the user's utterance.
- ``working`` (sub-mode ``thinking`` or ``delegating``) — STT done,
  Talker / orchestrator running.
- ``speaking`` — TTS audio flowing to the speaker.
- ``going_to_sleep`` — short transient between deferred-sleep
  trigger and the final flip back to ``sleeping``.

Sync state mutation, async publish discipline (architecture.md
§"Stable contracts"):

- All transition methods are ``async def`` because they ``await
  publisher.publish_activity(...)``.
- The state mutation itself is *sync* — between the precondition
  check and the new-state assignment there is no ``await``. Two
  concurrent callers can't race past the same precondition.
- Publishing happens AFTER the state has been written. A
  publisher failure (raises ``PublisherError``) leaves the FSM in
  the new state; the v1 fail-fast posture means the process
  crashes anyway and systemd resets, so we don't try to roll back.

Permissive on idempotent / strict on illegal:

- Idempotent same-state calls (e.g., ``on_speech_started`` while
  already in ``listening`` for continuous-conversation flow) are
  no-ops — no error, no publish.
- Genuinely illegal transitions (``on_first_audio_frame`` from
  ``sleeping``) raise :class:`VoiceAgentError`.

Mic-mode signal queue (Story 4.6's consumer):

- Single-writer (FSM), single-reader (audio transport).
- De-duped by tracking ``_last_mic_mode_emitted`` — only emits on a
  change so a turn that goes
  ``listening → working[thinking] → working[delegating] → speaking``
  doesn't enqueue four redundant ``vad_stt`` signals.

Deferred-sleep linchpin (FR46):

- ``on_tool_call_go_to_sleep()`` sets ``_sleep_pending = True`` and
  does NOT transition. The user's goodbye is still being synthesized
  to audio.
- On the next ``on_last_audio_frame()``: if ``_sleep_pending`` is
  set, transition ``speaking → going_to_sleep`` (publish) then
  immediately ``going_to_sleep → sleeping`` (publish). Mic flips
  to ``wake_word_only`` on the second transition.
- ``cancel_pending_sleep()`` clears the flag (e.g., a wake-word
  fires before ``on_last_audio_frame`` lands — rare race per FR46).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, Literal

import structlog

from voice_agent_pipeline.activity.states import ActivityState, WorkingSubmode
from voice_agent_pipeline.errors import VoiceAgentError
from voice_agent_pipeline.publisher.interface import EventPublisher
from voice_agent_pipeline.schemas.activity_event import ActivityEvent, ActivityPayload

log = structlog.get_logger(__name__)

#: Mic-mode signal type. Single source of truth — Story 4.6's
#: ``audio/transport`` consumer imports this directly.
MicMode = Literal["wake_word_only", "vad_stt"]

# Transition reasons live as a frozenset so a future contributor can
# grep them; the FSM passes the reason as a literal string in each
# transition method (no runtime check against this set — it's
# documentation + grep target).
_TRANSITION_REASONS: frozenset[str] = frozenset(
    {
        "startup_complete",
        "wake_detected",
        "end_of_speech",
        "end_of_user_speech",
        "orchestrator_dispatch",
        "first_audio_frame",
        "last_audio_frame",
        "deferred_sleep_complete",
    }
)


class ActivityFSM:
    """7-state activity FSM with deferred-sleep + mic-mode signaling.

    Single-writer: only this class mutates ``_current_state``. Other
    components emit transition events into the public ``on_*`` methods;
    they never touch the state field directly.

    Constructor accepts an optional ``on_sleeping_to_waking`` callback
    that fires on the ``sleeping → waking`` transition as a
    fire-and-forget background task. Story 4.5 wires this with the
    static-random wake-greeting orchestrator.
    """

    def __init__(
        self,
        publisher: EventPublisher,
        mic_mode_queue: asyncio.Queue[MicMode] | None = None,
        on_sleeping_to_waking: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        """Build the FSM.

        Args:
            publisher: The four-topic event publisher; the FSM calls
                ``publish_activity`` on every transition. Not optional —
                an FSM without a publisher would silently drop the
                ``ActivityEvent`` stream, which is a major regression
                from architectural intent.
            mic_mode_queue: Optional queue. When ``None``, the FSM
                constructs its own. Story 4.6 will inject one from
                outside so the audio transport can subscribe to the
                same queue.
            on_sleeping_to_waking: Optional callback fired as a
                background task when the FSM transitions
                ``sleeping → waking``. Story 4.5's wake-greeting
                orchestrator. Errors in the callback land in
                ``log.exception("activity.greeting_callback_error")``
                via the done-callback so they aren't silently lost.
        """
        self._publisher = publisher
        self._mic_mode_queue: asyncio.Queue[MicMode] = (
            mic_mode_queue if mic_mode_queue is not None else asyncio.Queue()
        )
        self._on_sleeping_to_waking = on_sleeping_to_waking
        self._current_state: ActivityState = "starting"
        self._working_submode: WorkingSubmode | None = None
        self._sleep_pending: bool = False
        # De-dup tracker for mic-mode emissions (architecture.md
        # §"Mic-mode signaling": "Track _last_mic_mode_emitted and only
        # enqueue on a change"). ``None`` until the first emit.
        self._last_mic_mode_emitted: MicMode | None = None

    # -----------------------------------------------------------------
    # Public read-only properties.
    # -----------------------------------------------------------------

    @property
    def current_state(self) -> ActivityState:
        """The state the FSM is currently in (read-only)."""
        return self._current_state

    @property
    def working_submode(self) -> WorkingSubmode | None:
        """``"thinking"`` or ``"delegating"`` while in ``working``; ``None`` otherwise."""
        return self._working_submode

    @property
    def sleep_pending(self) -> bool:
        """True if ``on_tool_call_go_to_sleep`` has fired but the deferred transition hasn't."""
        return self._sleep_pending

    @property
    def mic_mode_queue(self) -> asyncio.Queue[MicMode]:
        """The signal queue Story 4.6's audio transport subscribes to."""
        return self._mic_mode_queue

    # -----------------------------------------------------------------
    # Transition methods.
    # -----------------------------------------------------------------

    async def start(self) -> None:
        """Initial lifecycle transition ``starting → sleeping``.

        Called once by ``pipeline.py:run_pipeline`` after the publisher
        connects. Publishes the first ``ActivityEvent`` and emits the
        first mic-mode signal (``wake_word_only``).
        """
        self._guard_transition_from("starting", "start")
        self._current_state = "sleeping"
        self._working_submode = None
        await self._publish(
            from_state="starting",
            to_state="sleeping",
            transition_reason="startup_complete",
        )
        await self._emit_mic_mode("wake_word_only")

    async def on_wake_detected(self) -> None:
        """``sleeping → waking``. Wake-word fired; greeting + mic flip follow."""
        self._guard_transition_from("sleeping", "on_wake_detected")
        self._current_state = "waking"
        self._working_submode = None
        # Story 4.5: fire greeting as a background task BEFORE publish so
        # the ActivityEvent for ``waking`` doesn't lag behind greeting
        # latency (architecture.md: "ActivityEvent publishes immediately
        # on the FSM transition, NOT awaiting the greeting"). Done-
        # callback surfaces any exception via ``log.exception``.
        if self._on_sleeping_to_waking is not None:
            task = asyncio.create_task(self._on_sleeping_to_waking())
            task.add_done_callback(self._log_greeting_done)
        await self._publish(
            from_state="sleeping",
            to_state="waking",
            transition_reason="wake_detected",
        )
        await self._emit_mic_mode("vad_stt")

    async def on_speech_started(self) -> None:
        """``waking → listening`` on the first VAD speech after wake.

        Idempotent when already in ``listening`` (continuous-conversation
        flow: VAD detects new speech mid-conversation, FSM stays put).
        """
        if self._current_state == "listening":
            # Permissive idempotency — see class docstring.
            return
        self._guard_transition_from("waking", "on_speech_started")
        prior: ActivityState = self._current_state
        self._current_state = "listening"
        self._working_submode = None
        await self._publish(
            from_state=prior,
            to_state="listening",
            transition_reason="wake_detected",
        )

    async def on_speech_ended(self) -> None:
        """``listening → working[thinking]`` when VAD reports end-of-speech."""
        self._guard_transition_from("listening", "on_speech_ended")
        self._current_state = "working"
        self._working_submode = "thinking"
        await self._publish(
            from_state="listening",
            to_state="working",
            working_submode="thinking",
            transition_reason="end_of_user_speech",
        )

    async def on_dispatch_to_orchestrator(self) -> None:
        """``working[thinking] → working[delegating]`` (Story 4.7 slow-path entry)."""
        if self._current_state != "working":
            raise VoiceAgentError(
                reason="illegal_transition",
                current_state=self._current_state,
                attempted_method="on_dispatch_to_orchestrator",
            )
        if self._working_submode == "delegating":
            # Idempotent — already delegating.
            return
        self._working_submode = "delegating"
        await self._publish(
            from_state="working",
            to_state="working",
            working_submode="delegating",
            transition_reason="orchestrator_dispatch",
        )

    async def on_first_audio_frame(self) -> None:
        """``working → speaking`` on the first TTS audio frame leaving the transport."""
        self._guard_transition_from("working", "on_first_audio_frame")
        prior_submode = self._working_submode
        self._current_state = "speaking"
        self._working_submode = None
        await self._publish(
            from_state="working",
            to_state="speaking",
            transition_reason="first_audio_frame",
            # Carry the prior sub-mode forward in the log only — the
            # event payload's ``working_submode`` is None now.
            log_extra={"prior_working_submode": prior_submode},
        )

    async def on_last_audio_frame(self) -> None:
        """``speaking → listening`` (or → ``going_to_sleep`` → ``sleeping`` on deferred-sleep).

        The deferred-sleep linchpin lives here: if
        ``on_tool_call_go_to_sleep`` set ``_sleep_pending``, this
        transition fires the two-step ``speaking → going_to_sleep →
        sleeping`` chain (two ``ActivityEvent`` publishes, one mic-mode
        signal at the end).
        """
        self._guard_transition_from("speaking", "on_last_audio_frame")
        if self._sleep_pending:
            # Step 1: speaking → going_to_sleep
            self._current_state = "going_to_sleep"
            self._working_submode = None
            await self._publish(
                from_state="speaking",
                to_state="going_to_sleep",
                transition_reason="last_audio_frame",
            )
            # Step 2: going_to_sleep → sleeping (immediate)
            self._current_state = "sleeping"
            self._sleep_pending = False
            await self._publish(
                from_state="going_to_sleep",
                to_state="sleeping",
                transition_reason="deferred_sleep_complete",
            )
            await self._emit_mic_mode("wake_word_only")
        else:
            self._current_state = "listening"
            self._working_submode = None
            await self._publish(
                from_state="speaking",
                to_state="listening",
                transition_reason="last_audio_frame",
            )

    async def on_going_to_sleep_complete(self) -> None:
        """Explicit ``going_to_sleep → sleeping`` transition.

        Most callers go through the deferred-sleep path inside
        :meth:`on_last_audio_frame`; this method exists as a separate
        seam so unit tests can exercise the transition directly without
        re-driving the entire deferred-sleep chain.
        """
        self._guard_transition_from("going_to_sleep", "on_going_to_sleep_complete")
        self._current_state = "sleeping"
        self._working_submode = None
        self._sleep_pending = False
        await self._publish(
            from_state="going_to_sleep",
            to_state="sleeping",
            transition_reason="deferred_sleep_complete",
        )
        await self._emit_mic_mode("wake_word_only")

    def on_tool_call_go_to_sleep(self) -> None:
        """Schedule a deferred-sleep transition (FR46).

        Sets ``_sleep_pending = True`` without changing state. The next
        ``on_last_audio_frame`` will fire the deferred-sleep chain.
        Sync because we don't publish here — no ``ActivityEvent`` for
        the tool call itself; only the eventual transitions publish.
        """
        log.info(
            "activity.sleep_scheduled",
            current_state=self._current_state,
            working_submode=self._working_submode,
        )
        self._sleep_pending = True

    def cancel_pending_sleep(self) -> None:
        """Clear ``_sleep_pending`` (e.g., wake-word fires mid-deferred-sleep).

        No-op if the flag isn't set. Sync — same rationale as
        :meth:`on_tool_call_go_to_sleep`.
        """
        if not self._sleep_pending:
            return
        log.info(
            "activity.sleep_cancelled",
            cancelled_at_state=self._current_state,
        )
        self._sleep_pending = False

    # -----------------------------------------------------------------
    # Internal helpers.
    # -----------------------------------------------------------------

    def _guard_transition_from(self, expected: ActivityState, attempted_method: str) -> None:
        """Raise :class:`VoiceAgentError` if the FSM isn't in ``expected``.

        v1 fail-fast: illegal transitions are programming errors (bad
        wiring at the call site), not external-service failures. Crash
        the process; systemd restarts. CLAUDE.md rule #4.
        """
        if self._current_state != expected:
            raise VoiceAgentError(
                reason="illegal_transition",
                current_state=self._current_state,
                attempted_method=attempted_method,
                expected=expected,
            )

    async def _publish(
        self,
        *,
        from_state: ActivityState,
        to_state: ActivityState,
        transition_reason: str,
        working_submode: WorkingSubmode | None = None,
        log_extra: dict[str, object] | None = None,
    ) -> None:
        """Build the :class:`ActivityEvent`, log INFO, and ``await`` publish.

        The event publisher's ``publish_activity`` may raise
        :class:`PublisherError` — let it propagate (CLAUDE.md rule #4).
        """
        event = ActivityEvent(
            payload=ActivityPayload(
                state=to_state,
                from_state=from_state,
                working_submode=working_submode,
                transition_reason=transition_reason,
            )
        )
        log_fields: dict[str, object] = {
            "from_state": from_state,
            "to_state": to_state,
            "working_submode": working_submode,
            "transition_reason": transition_reason,
        }
        if log_extra is not None:
            log_fields.update(log_extra)
        log.info("activity.transition", **log_fields)
        await self._publisher.publish_activity(event)

    async def _emit_mic_mode(self, mode: MicMode) -> None:
        """Enqueue a mic-mode signal — de-duped against the last emission.

        The de-dup invariant keeps the queue short for Story 4.6's
        consumer: a turn that traverses
        ``listening → working[thinking] → working[delegating] → speaking``
        doesn't enqueue four redundant ``vad_stt`` signals. Only state
        transitions that cross the wake-word/AWAKE boundary produce
        actual mic-mode flips.
        """
        if self._last_mic_mode_emitted == mode:
            return
        self._last_mic_mode_emitted = mode
        await self._mic_mode_queue.put(mode)

    def _log_greeting_done(self, task: asyncio.Task[None]) -> None:
        """Done-callback for the ``on_sleeping_to_waking`` background task.

        ``asyncio`` swallows uncaught task exceptions silently — the
        callback re-raises inside a try/except so any failure in
        Story 4.5's greeting orchestrator surfaces in logs.
        """
        try:
            task.result()
        except Exception:
            log.exception("activity.greeting_callback_error")
