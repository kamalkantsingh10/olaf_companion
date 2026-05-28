# Component Brief: olaf-dashboard

**Parent project:** OLAF Companion (Personal Voice Agent)
**Status:** Spec phase (no implementation yet)
**Author:** Kamal
**Last updated:** 2026-05-28
**Audience:** LLM coding partner (Claude Code) implementing the dashboard service, plus humans who will fork or extend it
**Pairs with:** [voice-agent-pipeline-brief.md](voice-agent-pipeline-brief.md) — the publisher side (the dashboard reads this project's logs). [olaf-embodiment-brief.md](olaf-embodiment-brief.md) — the parallel sibling project (the dashboard *also* reads the body's logs, when the body project ships logging).
**Promoted from:** [decision-records.md](decision-records.md) §DR-003 (frozen 2026-05-26).

---

## Executive Summary

`olaf-dashboard` is the sibling project that surfaces what OLAF is doing in
real time on a web screen. It is a **pure consumer** — it tails the
structured JSON logs that the pipeline (and, when ready, the body) already
write, reconstructs turns and expression state from those events, and serves
the data to a browser over a local-only WS/SSE bridge. It writes nothing
back to the pipeline, never appears on the DDS bus, and never enters the
voice-loop hot path.

The architectural posture is the consumer side of the pipeline's
agnostic-publisher boundary. DR-003 froze it in this shape because
everything the dashboard needs is **already logged** — turn boundaries,
transcripts (INFO-level redacted), responses, latency numbers, mood and
activity transitions, speech-emotion and vocalization events. Building
this as a log-tail consumer means **zero pipeline change**, best privacy
posture, and a unified surface that absorbs body-side signals the same way
once the body project logs them. A versioned `events.jsonl` structlog sink
in the pipeline (Story v2-1) is documented as the **promotion path** if
the unversioned-log-format coupling starts to bite — but it is not adopted
preemptively.

This component is the **only** part of the OLAF companion that has a UI.
Pipeline and body deliberately don't. That separation — the realtime
voice/expression surface vs the observability/inspection surface — is the
brief's most important contract.

## The Problem

The pipeline runs as a background systemd service. When something feels
off mid-conversation — opener fired wrong, mood drifted, mic mode flipped
unexpectedly, a turn took 3 seconds when it should have taken 1 — there is
no live view into *why*. You SSH in and `tail -f voice-agent.log`, parse
JSON in your head, and try to correlate events across millisecond
timestamps. That works for one developer with the codebase loaded, but:

1. **No turn structure.** The log is a flat stream. Reconstructing what
   the user said → how the pipeline answered → what expression events
   fired → how long each step took is a manual exercise every time.
2. **No body-side signal.** When the body finally logs its own actions
   ("wake animation done", "emphasis nod fired", "led pulse complete"),
   it lives in *its* log on *its* host. Cross-correlating with the
   pipeline means manually merging two timelines.
3. **Latency is hidden.** The Story 6.5 / NFR35 instrumentation
   (`stt_ms`, `ttft_ms`, `ttfb_ms`, `end_to_first_real_audio_ms`,
   `opener_*`) lives in the log but is invisible until you grep for it.
   The DR-001 v2 timeline targets (~1.7–2.0 s end-to-end) need a
   continuous visual signal to validate.
4. **Demos and user studies need this surface.** The paper that DR-001
   and DR-002 contribute to is partly an evaluation study;
   facilitator-facing live dashboards + session capture/replay are part
   of the methodology.

A pretty live web screen solves all four with one consumer.

## The Solution

A long-running Python service (FastAPI / Starlette, asyncio) that:

1. **Tails N structured JSON log files** — `voice-agent.log` (INFO) from
   this pipeline now; the body's log (when the body project ships
   logging) later. Rotation-aware tailing (the pipeline uses
   `RotatingFileHandler`; the tailer must follow across rename/truncate,
   not just `seek(end)`).
2. **Reconstructs turn-by-turn structure** by grouping events between
   each `vad.utterance.started` (or `wakeword.detected`) and the next
   `fsm.audio_flight.entered_listening` — that's one turn. Within a
   turn, pulls the transcript (DEBUG-gated; INFO has `clarification.picked`
   / `greeting.picked` / synthesised-response text), the response, any
   `speech_emotion` / `vocalization` events, the `mood` / `activity`
   transitions, and the latency block from `turn.complete` (post-Epic-6
   only — pre-Epic-6 you reconstruct deltas from raw event timestamps).
3. **Serves a single-page web app** with five sections:
   - **Conversation** — turn-by-turn transcript ↔ response, latest at top.
   - **Expression state** — current `mood` + `activity` + last
     `speech_emotion` + any in-flight `vocalization`.
   - **Body status** — only when the body's log is reachable: latest
     gesture rendered, current pose, any `embodiment.unmapped_vocalization`
     warnings.
   - **Latency / metrics** — last-N-turn rolling p25 / 50 / 75 / 90 for
     `stt_ms`, `ttft_ms`, `ttfb_ms`, `end_to_first_real_audio_ms`,
     `opener_onset_ms`, `dead_air_after_opener_ms`,
     `emphasis_count_per_sentence`.
   - **Raw event timeline** — unfiltered scrollback of every log event
     in arrival order with the source file labelled (pipeline / body).
4. **Streams updates to the browser** over a custom WS/SSE bridge
   (DR-003 Fork 2 lean: custom WS/SSE beats rosbridge for this — we
   already have JSON events, no need for the full ROS web stack).
   Bind to localhost or LAN-only; transcripts are visible.

That is the entire scope. The dashboard does not transcribe, reason,
publish, persist conversation state, or do anything that could affect
the pipeline. It is read-only and side-effect-free with respect to the
voice loop.

## What Makes This Different

Architectural decisions an LLM implementing this component **must not violate**:

1. **Pure consumer. Never publish back.** The dashboard does not enter
   the DDS bus, does not call any pipeline / body HTTP endpoint, and
   does not modify any pipeline / body file. If a user wants to "send
   feedback to OLAF" from the dashboard, that becomes a *different*
   topic on a *different* project (a future intent-input channel),
   never a write into the pipeline's contract. Single-writer per topic
   is the invariant that prevents drift across the whole companion.
2. **Passive — must NEVER affect the pipeline.** Separate process,
   separate venv, separate failure domain. If the dashboard crashes,
   pipelines and body keep running. If the pipeline crashes, the
   dashboard shows stale state with a visible "disconnected" banner.
   The pipeline's fail-fast posture (CLAUDE.md rule #4) does not
   extend to the dashboard.
3. **Read INFO log, not DEBUG.** `voice-agent.log` is INFO+. `debug.log`
   carries more under `LOG_LEVEL=DEBUG` including raw transcripts (FR39
   gated). Reading INFO keeps the privacy exposure level the pipeline
   has already accepted; reading DEBUG silently widens it. If the
   dashboard wants the user transcript, it should rely on the
   INFO-level structured events that already carry it (e.g., the
   redaction processor lets `transcript` pass at INFO only when an
   event is explicitly tagged for it).
4. **Log format is an unversioned contract — handle the rename.**
   Today, the pipeline can rename a log event (`activity.transition` →
   `activity_transition`) without bumping anything. The dashboard
   must (a) document the event names it consumes in its own README,
   (b) fail loudly on rename rather than silently miss events
   ("known-event allowlist + log unknown-event-name WARN once per
   process"), (c) be prepared to migrate to `events.jsonl` (Story v2-1)
   when that promotion path activates.
5. **Rotation-aware tail.** `RotatingFileHandler` renames the active
   file on roll-over (`voice-agent.log` → `voice-agent.log.1`) and
   reopens a fresh `voice-agent.log`. A naïve `tail -f` style follower
   misses everything after the rename. Implementation must detect
   rename/truncate (inode change, file size shrink) and re-open the
   path. Standard `watchfiles` / `aiofiles` patterns work; document the
   chosen pattern.
6. **Localhost or LAN-only — transcripts are visible.** No cloud, no
   tunneling, no internet exposure. The local privacy exposure level
   inherits from the INFO log: transcripts can appear in the
   conversation pane. That is fine on a home network with one user;
   any deployment beyond that needs an explicit re-decision.
7. **Single-page, single-user.** No auth, no multi-tenant, no session
   model. One Kamal, one browser tab open against `localhost:<port>`.
   If that ever changes, the privacy + auth design needs revisiting.
8. **Body log access stays local.** When the body lives on a separate
   Pi (future), its log lives on that Pi. The dashboard reaches it via
   NFS, ssh, or a log shipper (rsyslog, vector) — not by adding a
   network endpoint to the body. The body remains a pure consumer of
   the pipeline plus a hardware controller; it does not host the
   dashboard or even respond to dashboard pings.

## Stakeholders & Consumers

This project consumes logs; it has no consumers of its own beyond Kamal's
browser.

- **Kamal** — primary user, primary developer. Watches the dashboard
  during conversation soaks, demos, and debugging sessions.
- **voice-agent-pipeline** — the primary log source. The dashboard's
  contract is to *that project's* `./logs/voice-agent.log`.
- **olaf-embodiment** — the secondary log source (once the body project
  ships JSON logging). DR-003 §"What data exists today" calls this out:
  "Body must emit signals → body writes its own log" — no new ROS 2
  topic from the body for the dashboard to consume.
- **Future paper / user-study facilitation** — the dashboard doubles
  as a live demo panel and a session-capture / replay surface. DR-001
  and DR-002 both want the latency + emphasis-density numbers visible
  during evaluation.

## Success Criteria

A dashboard feels *useful* when these hold:

- **Live within 200 ms of the pipeline.** A log event written by the
  pipeline appears in the raw-event timeline pane within 200 ms p95.
  (File-tail + WS push is well under this; the bound is the browser
  render loop, not the consumer.)
- **Turn reconstruction is correct.** Across a 30-min session, every
  user utterance lands as a single turn block with its transcript /
  response / latency numbers grouped — no orphan transcripts, no
  cross-turn bleed. Edge cases: the user says nothing
  (`sequential_loop.no_speech_retry`), the pipeline times out
  (`vad.timeout`), an interrupt fires (post-v1.5 barge-in) — each
  surfaces as a labelled turn-state in the UI, not silently lost.
- **Latency targets visible and tracked.** Rolling p50 / p95 for the
  Story-6.5 instrumentation fields displays continuously; missing the
  NFR1 / NFR33 / NFR34 thresholds shows a red badge in the latency
  pane.
- **Rotation survival.** Through a soak that crosses a log roll-over
  (default 50 MB / file, 7 days retention), the dashboard keeps showing
  live events without manual restart.
- **No pipeline impact.** Running the dashboard at full speed during
  a 30-min conversation does not change measured NFR1 / NFR2 / NFR5
  numbers from a no-dashboard baseline. (CPU + filesystem read of the
  log should be negligible; verify in soak.)
- **Disconnected state is honest.** If the pipeline stops writing
  (process down, log path moved), the dashboard shows a visible
  "disconnected from pipeline" banner within 5 s, not a stale-but-fresh-
  looking display.

## Scope

**In scope (v1):**
- Tail `voice-agent.log` (INFO) from the local pipeline install.
- Turn-by-turn reconstruction across the canonical event set (documented
  in Appendix A.2).
- Five UI sections (conversation / expression / body-stub /
  latency / raw timeline).
- WS or SSE bridge between server and browser; bind localhost or LAN.
- Rotation-aware tailer.
- Known-event allowlist with WARN-on-unknown.

**In scope (v1.5 — when body ships JSON logging):**
- Tail the body's log alongside the pipeline log (path configurable;
  same posture as the pipeline log).
- Body-status pane populated with the body's logged events
  (gesture rendered, pose state, unmapped-vocalization WARN, etc.).
- Cross-source turn correlation (pipeline turn ID + body event
  timestamps).

**In scope (v2 — when the pipeline ships Story v2-1):**
- Migrate from `voice-agent.log` (INFO, unversioned format) to
  `events.jsonl` (versioned). Same posture; new file path; the
  unknown-event allowlist becomes a `schema_version`-gated parser.

**Out of scope (forever, by design):**
- Anything that writes back into the pipeline's or body's contract.
  Touch input / camera input / user feedback / settings changes are
  *separate* projects on their own (future) topics.
- Voice synthesis, transcription, reasoning, mood control, expression
  decisions — all owned by the pipeline / orchestrator.
- Cloud or remote-access deployment (out of v1 for privacy + auth
  reasons).
- Authentication and multi-user — single-user local-only.
- Long-term conversation persistence / search / database. The log
  rotation window (default 7 days) is the persistence boundary.
  Longer-term storage is a separate concern.

**Deferred to v2 / v3:**
- Session capture / replay (re-feed an archived log into the
  dashboard for paper figures or user-study playback). Likely belongs
  in the dashboard, just not in v1.
- Remote-access mode (mTLS + auth, tunneled) for showing-off
  deployments. Re-decide privacy + auth before adopting.
- Embedded body-side controls (a "pet OLAF" button that
  triggers a touch event). That's a new topic on a new project.

## Deployment Platform

**v1 target: same host as the pipeline.** Local Linux PC. The dashboard
runs as a separate systemd service (e.g.,
`olaf-dashboard.service`, `WorkingDirectory` pinned to its repo,
`Restart=on-failure`, same conventions as the pipeline's unit). Reads
the pipeline's `./logs/voice-agent.log` directly (local FS path).
Browser connects to `localhost:<port>`. Port configured in the
dashboard's own `setup.toml` (a separate file — the dashboard does NOT
edit or read the pipeline's `setup.toml`).

**v1.5 multi-host (future):** when the body moves to a separate Pi,
the body's log is on the Pi. Options:
- (a) Mount the Pi's log directory over NFS — dashboard tails as if
  local.
- (b) Run a log-shipper (rsyslog, vector, journalbeat) that ships the
  body's log to the host running the dashboard.
- (c) Stand up a tiny ssh-based tailer.

Choose at v1.5 time; DR-003 doesn't pre-decide.

**Single-host is the v1 default** and resolves the cross-host-clock-skew
question DR-002 also faces — for the dashboard, the clock skew doesn't
matter much (it's not anchored to audio frames like NFR5) but it does
matter for the raw timeline's intra-event ordering correctness.

## Risks

- **Log format breakage.** The pipeline can rename an event today
  without any signal. Mitigation: allowlist + WARN-on-unknown +
  document the consumed event set; migrate to versioned
  `events.jsonl` (Story v2-1) when format-rename pain manifests.
- **Rotation-tail bugs.** Naïve `seek(end)` followers silently miss
  data across rolls. Mitigation: use a battle-tested
  rotation-aware tailing library or a clearly-implemented inode-watch
  pattern; integration test exercises a forced rotation mid-session.
- **Browser-side back-pressure.** A long soak generates thousands of
  events; the WS stream can outrun the browser's render loop and
  cause memory pressure. Mitigation: server-side ring buffer (e.g.,
  last 2 000 events), browser-side rolling-window display (e.g., last
  500 in the raw timeline), aggressive `requestAnimationFrame`
  batching.
- **Privacy posture creep.** Once the dashboard is fun, the temptation
  to "just expose it to my phone over the LAN" is real. Mitigation:
  v1 binds localhost by default; LAN binding requires a `setup.toml`
  knob change and a printed warning at startup. Cloud / WAN exposure
  requires re-deciding the brief.
- **Dashboard masks pipeline issues.** Pretty UI hides ugly numbers.
  Mitigation: latency-threshold red badges, unknown-event count
  badges, log-tail-disconnect banners — make the bad cases visible
  rather than smooth.

---

## Appendix A — Interface Contract

The wire is the log. This appendix captures the consumed surface as it
stands at `voice-agent-pipeline` HEAD (commit at brief landing time).

### A.1 — Log file location + format

| Setting | Value | Source |
|---|---|---|
| Log path (pipeline) | `<pipeline-repo>/logs/voice-agent.log` | `logging/setup.py`, configurable in pipeline's `setup.toml` |
| Format | JSON, one event per line, structlog-shaped | architecture.md §Logging Conventions |
| Level | INFO+ | FR37, NFR25 |
| Rotation | `RotatingFileHandler`, size-based (default 50 MB), N retained (default 7 days) | architecture.md §Logging |
| Encoding | UTF-8 | structlog default |
| Body log path (future v1.5) | `<body-repo>/logs/olaf-embodiment.log` | TBD by body project |

### A.2 — Canonical event names (consumed by v1 dashboard)

These are the events the v1 pipeline emits today. The dashboard's known-event
allowlist is **this set**; an unknown event name surfaces as
`dashboard.unknown_event` WARN once per name per process.

**Lifecycle / FSM:**
- `pipeline.started`, `pipeline.stopped`
- `startup.completed`, `startup.validated.*` (audio / cartesia / stt /
  talker / wakeword / audio_assets)
- `activity.transition` — `from_state`, `to_state`, `working_submode`,
  `correlation_id`
- `fsm.audio_flight.entered_listening`, `fsm.audio_flight.entered_speaking`
- `mic_mode.transition` — `wake_word_only` ↔ `vad_stt`
- `sequential_loop.ready`, `sequential_loop.sleeping`,
  `sequential_loop.stopped`, `sequential_loop.no_speech_retry`
- `pipeline.shutdown_signal` (when implemented)

**Wake / VAD / STT:**
- `wakeword.detected`, `wakeword.buffer_cleared`, `wakeword.waiting`,
  `wakeword.processor.stopped`
- `vad.utterance.started`, `vad.state_reset`, `vad.timeout`
- `vad.utterance.captured` (DR-003 references this as turn boundary;
  verify exact event name at landing time)
- `stt.model_loaded`, `stt.groq.load.noop`
- `stt.transcript` (DEBUG-gated under FR39; dashboard pulls from
  INFO-level summary if available, otherwise reconstructs from
  `clarification.picked` / `talker.responded` / `talker.tool_call_*`)

**Talker / Tools:**
- `talker.tool_call_unsupported_type`, `tool.dispatch`,
  `tool.dispatch_unknown_name`
- `talker.responded` (verify exact name; the talker's spoken-text
  emission event)

**TTS / Audio:**
- `tts.sentence_speak`, `tts.first_frame` (verify), `tts.ttfb_ms`
  (currently a field on `tts.first_frame`; promotes to
  `turn.complete.ttfb_ms` in Epic 6 / Story 6.5)
- `cached_audio.play.start`
- `clarification.picked`, `greeting.picked`, `greeting.injected`,
  `goodbye.picked`, `filler.no_pick`
- `audio.frame_counter` (low-volume; informational)

**Mood / Vocalization / Embodiment Events:**
- `mood.publish`, `mood.publish_initial`
- `vocalization.unmapped` (the pipeline's WARN when a vocalization
  payload tag is outside the canonical set; dashboard highlights as
  warning chip)
- `speech_emotion.*` events (if the pipeline logs them; today the
  publisher path may not emit a structured log per event — dashboard
  reads from the DDS-publish event if logged, otherwise computes from
  splitter logs)

**Orchestrator (slow path):**
- `orchestrator.dispatch_begin`, `orchestrator.turn_end`,
  `orchestrator.missing_turn_end`

**Publisher / Wire:**
- `publisher.connect_failed`, `publisher.disconnected`,
  `publisher.disconnect_warning`

**Daemon:**
- `daemon.disabled`

**Epic 6 additions (when post-v1 v2 Expression lands):**
- `turn.complete` — the v2 latency-instrumentation summary event
  (Story 6.5 / NFR35) carrying `stt_ms`, `ttft_ms`, `ttfb_ms`,
  `end_to_first_real_audio_ms`, `opener_source`, `opener_duration_ms`,
  `dead_air_after_opener_ms`, `emphasis_count_per_turn`. This is the
  single richest event for the latency / metrics pane.

> **Confirm at implementation time.** Event names evolve; the dashboard
> should pin against the pipeline's HEAD at its own first commit, then
> document drift handling in its README.

### A.3 — Turn reconstruction algorithm

```
turn_boundary  = vad.utterance.started  (NEW turn opens)
              | wakeword.detected      (NEW turn — wake-greeting turn)
              | fsm.audio_flight.entered_listening  (PRIOR turn closes; new turn awaits VAD)

within a turn, collect:
  stt:        stt.transcript                        (DEBUG; alt: reconstruct from talker.responded text)
  routing:    activity.transition working[submode]   (thinking | delegating)
  response:   talker.responded text, cached.* picks, orchestrator events
  emotion:    speech_emotion events (from publisher logs)
  vocalization: vocalization events (incl. v2 `emphasis`)
  latency:    turn.complete fields (v2) | derive from per-event timestamps (pre-v2)
  outcome:    fsm.audio_flight.entered_listening (success) | vad.timeout (no-input) | sequential_loop.no_speech_retry (retry)
```

A turn block in the UI groups these and renders the latency strip + transcript ↔ response.

### A.4 — `correlation_id` (turn binding)

When present on an event, `correlation_id` carries the same UUID across
all audio-anchored / FSM-driven events for one turn. The dashboard
should use this for cross-event grouping in preference to timestamps
when available (it is unambiguous; timestamp grouping can confuse on
rapid back-to-back turns).

### A.5 — Schema versioning posture

Today: the log format is **unversioned**. The dashboard's known-event
allowlist is the de-facto schema.

Story v2-1 (parked in v2 backlog): adds `events.jsonl` with a
`schema_version` field. Migration plan when v2-1 activates:
- Dashboard config gains `[source] format = "voice-agent-log" | "events-jsonl"` (default unchanged).
- When set to `events-jsonl`, the dashboard parses the versioned stream and rejects mismatched versions at startup (mirroring the pipeline's `SchemaVersionError` discipline).
- The pipeline's `voice-agent.log` continues to exist for human reading; the dashboard reads whichever the operator points it at.

---

## Appendix B — Recommended Implementation Shape

This is a *recommendation*. The implementer may deviate — these
defaults make the common case fast.

### B.1 — `setup.toml` (dashboard-side; mirrors the pipeline's split)

```toml
schema_version = 1                   # dashboard's own version, independent

[server]
host = "127.0.0.1"
port = 7100                          # arbitrary; pick what isn't in use
log_level = "INFO"

[sources.pipeline]
log_path = "../olaf_companion/logs/voice-agent.log"   # relative or absolute
known_event_allowlist_path = "config/known_events.txt"  # one event name per line

[sources.body]
enabled = false                       # flip to true once body ships logging
log_path = "../olaf-embodiment/logs/olaf-embodiment.log"

[ui]
turn_window_size = 50                 # last N turns rendered in conversation pane
raw_timeline_window_size = 500        # last N raw events
latency_rolling_window = 30           # rolling p50/p95 over last N turns
```

### B.2 — Process layout

```
olaf-dashboard/
├── README.md
├── pyproject.toml                   # uv + ruff + pyright config
├── uv.lock
├── justfile                         # run / check / test / serve
├── setup.toml                       # service config (above)
├── config/
│   └── known_events.txt             # allowlist (one event name per line)
├── src/
│   └── olaf_dashboard/
│       ├── __main__.py              # asyncio.run(serve())
│       ├── server.py                # FastAPI app + WS endpoint
│       ├── tailers/
│       │   ├── rotating_tail.py     # rotation-aware async file tail
│       │   ├── pipeline.py          # specialises for pipeline log shape
│       │   └── body.py              # specialises for body log shape (v1.5)
│       ├── turn.py                  # turn-reconstruction state machine
│       ├── allowlist.py             # known-event check + WARN-on-unknown
│       └── web/                     # static HTML / JS / CSS for the SPA
├── tests/
│   ├── unit/
│   │   ├── test_rotating_tail.py    # forced rotation mid-session
│   │   ├── test_turn_reconstruction.py  # fixture log → expected turns
│   │   └── test_allowlist.py
│   └── integration/
│       └── test_pipeline_replay.py  # replay a real captured log → dashboard state
└── deploy/
    └── systemd/
        └── olaf-dashboard.service
```

### B.3 — Toolchain

Same defaults as the pipeline: `uv`, `ruff`, `pyright`, `pytest`,
`pytest-asyncio`, `structlog`, `pydantic` v2 for any config / event
shapes. Plus `fastapi` + `uvicorn` + a tiny SPA (vanilla HTML/JS or
HTMX — keep it light, the dashboard is not a complex front-end).

### B.4 — Startup discipline (mirrors the pipeline)

1. Load `setup.toml` + `config/known_events.txt`.
2. Validate the pipeline log path exists and is readable. Refuse to
   start otherwise (fail-fast).
3. Validate the body log path (if `[sources.body] enabled = true`).
4. Open the WS bind socket on the configured port. Refuse to start if
   bind fails (fail-fast).
5. Start the tailer(s) in the background. Push events through the
   turn-reconstructor and the WS broadcaster.
6. Render `running` and emit `dashboard.started` to its OWN structured
   log (yes, the dashboard logs too — keeps the parity story clean).

Same posture as the pipeline (CLAUDE.md rule #4): missing deps at
startup → refuse to start; runtime failures → crash → systemd restart.

### B.5 — What's intentionally NOT in this brief

- Visual design / UI mockups — that's a sketch the implementer owns,
  not a contract.
- Specific framework choices (FastAPI vs Starlette vs aiohttp,
  vanilla JS vs HTMX vs htmx-equivalent) — the recommendation above
  is the lowest-risk default, the implementer picks the actual stack
  on first-commit.
- Capture / replay logic — deferred to v2 / v3.
- Authentication — deferred until the deployment model demands it.

---

*This brief lives in the `voice-agent-pipeline` repo because that's
where the wire (= log format) is authored — the brief travels WITH
the contract. When the dashboard project is set up as a sibling repo,
this file should be copied to its `docs/` and kept in sync via a
tagged release of the pipeline.*
