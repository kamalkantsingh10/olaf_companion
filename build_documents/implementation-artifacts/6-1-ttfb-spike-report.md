# Story 6.1 - Cartesia TTFB measurement spike (WebSocket transport)

Generated: `2026-05-28T09:03:45.723248+00:00` -> `2026-05-28T09:15:11.513793+00:00`

## Run metadata

- voice_id: `6ccbfb76-1fc6-48f7-b71d-91ac6298247b`
- model: `sonic-3`
- transport: `websocket`
- cold samples requested: `250` (successful: `250`, errors: `0`)
- warm samples requested: `250` (successful: `250`, errors: `0`)
- inter-request sleep: `75 ms`
- dev host: `OneHumanCompany / Linux 6.17.0-29-generic / python 3.12.3`
- Cartesia SDK: `3.0.2`
- Pipeline schema_version: `3`

## TTFB statistics (overall + by mode)

All values in milliseconds. `mean +/- stdev` shows the distribution shape; percentiles characterise the tail.

| group | n | mean +/- stdev | p25 | p50 | p75 | p90 | p95 | p99 | min | max |
|---|---|---|---|---|---|---|---|---|---|---|
| overall | 500 | 360 +/- 141 | 230 | 383 | 467 | 517 | 563 | 789 | 160 | 879 |
| cold (one WS per call) | 250 | 487 +/- 79 | 443 | 467 | 506 | 563 | 619 | 843 | 382 | 879 |
| warm (WS reused) | 250 | 234 +/- 41 | 206 | 230 | 252 | 283 | 309 | 361 | 160 | 436 |


Warm-vs-cold median delta: **237 ms** (cold 467 ms - warm 230 ms). This is the amortised TLS + WS-upgrade cost paid on every cold call; a future connection-pool change would recover this on every turn after the first.

## TTFB stratified by transcript length

Buckets: short = 1-5 words; medium = 6-15 words; long = 16+ words.

### Cold path

| bucket | n | mean +/- stdev | p25 | p50 | p75 | p90 | p95 | p99 | min | max |
|---|---|---|---|---|---|---|---|---|---|---|
| short | 83 | 486 +/- 68 | 452 | 467 | 500 | 542 | 621 | 718 | 384 | 843 |
| medium | 93 | 493 +/- 85 | 441 | 472 | 515 | 573 | 618 | 846 | 382 | 875 |
| long | 74 | 480 +/- 83 | 437 | 457 | 500 | 550 | 592 | 834 | 388 | 879 |

### Warm path

| bucket | n | mean +/- stdev | p25 | p50 | p75 | p90 | p95 | p99 | min | max |
|---|---|---|---|---|---|---|---|---|---|---|
| short | 80 | 233 +/- 43 | 204 | 228 | 254 | 279 | 290 | 361 | 165 | 436 |
| medium | 85 | 236 +/- 37 | 210 | 237 | 250 | 279 | 305 | 348 | 160 | 367 |
| long | 85 | 232 +/- 44 | 203 | 227 | 250 | 289 | 310 | 362 | 164 | 396 |

## DR-001 OPTION D VIABILITY

Overall median TTFB on WebSocket = **383 ms** (<= 400 ms threshold).

Story 6.2 should re-evaluate whether the cached-opener subsystem is still justified versus pure-live Cartesia synthesis. See DR-001 'Open question (keystone)' - this measurement closes the question on the WS-viable side. The perceived-latency gap may close without pre-cached opener audio, especially if a future change pools WS connections (see the warm-vs-cold delta in this report).

## SSE comparison band (from DR-001 'Empirical evidence')

| percentile | SSE production logs (n=308) |
|---|---|
| p50 | ~1070 ms |
| p75 | ~1640 ms |

If the WS overall p50 above is materially different from ~1070 ms, that is the SSE -> WS transport delta - call it out in any follow-up commit referencing this report.

## Methodology

- Single-clock per-request measurement: `t_send = time.time_ns()` after `await conn.send(...)`; `t_first = time.time_ns()` on the first `Chunk` event arrival; `ttfb_ms = (t_first - t_send) // 1_000_000`. Matches the production `tts.first_frame.ttfb_ms` log shape, so directly comparable to DR-001's SSE baseline.
- Cold path: open `client.tts.websocket_connect()` per request, close on first chunk. One TLS handshake + WS upgrade per call.
- Warm path: open one WS, send N requests serially with fresh `context_id` per request; the connection persists across the phase. Amortises transport-side overhead.
- Inter-request sleep: 75 ms - stays well below Cartesia's documented per-second cap on the Sonic-3 paid tier so the spike does not measure rate-limit inflation.
- Transcript pool: 20 phrases (7 short, 7 medium, 6 long) randomly cycled. Random pick per request defeats same-text response caching on the Cartesia side.
- Audio format pinned to 16 kHz mono S16LE - same format the rest of the pipeline uses end-to-end. No resampling artefacts in the spike.
- WS path forces `add_timestamps=True`, `add_phoneme_timestamps=False` - matches the Story 6.1 v1 runtime shape (the spike measures what the production WS callers will see).

## Raw samples

Each row: `mode,ttfb_ms,word_count,bucket,transcript` (in capture order; cold phase first, then warm).

```
cold,818,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,507,28,long,If you could pick any single place in the world to visit next weekend, and money
cold,571,11,medium,There's a small park about three blocks away from the apartment.
cold,482,11,medium,I was hoping we could grab dinner together later this evening.
cold,450,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,487,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
cold,464,4,short,Hmm, let me think.
cold,410,10,medium,The kitchen is on the left side of the hallway.
cold,511,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,522,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,522,9,medium,Have you read that book I mentioned yesterday afternoon?
cold,843,11,medium,There's a small park about three blocks away from the apartment.
cold,433,11,medium,It rained heavily for most of the afternoon, then cleared up.
cold,596,8,medium,I think the weather is quite nice today.
cold,432,3,short,Hello there friend.
cold,471,2,short,Yes, exactly.
cold,499,4,short,Hmm, let me think.
cold,476,3,short,Are you well?
cold,485,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,843,3,short,Hello there friend.
cold,485,9,medium,Have you read that book I mentioned yesterday afternoon?
cold,476,3,short,Are you well?
cold,436,10,medium,The kitchen is on the left side of the hallway.
cold,463,4,short,Hmm, let me think.
cold,537,4,short,Hmm, let me think.
cold,518,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
cold,506,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
cold,461,3,short,I'm not sure.
cold,485,28,long,If you could pick any single place in the world to visit next weekend, and money
cold,730,11,medium,Could you please pass the salt and pepper across the table?
cold,515,8,medium,I think the weather is quite nice today.
cold,495,2,short,Yes, exactly.
cold,508,10,medium,The kitchen is on the left side of the hallway.
cold,481,10,medium,The kitchen is on the left side of the hallway.
cold,490,11,medium,I was hoping we could grab dinner together later this evening.
cold,457,3,short,Hello there friend.
cold,477,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
cold,473,11,medium,It rained heavily for most of the afternoon, then cleared up.
cold,610,9,medium,Have you read that book I mentioned yesterday afternoon?
cold,432,11,medium,Could you please pass the salt and pepper across the table?
cold,428,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
cold,690,3,short,Tell me more.
cold,454,3,short,Tell me more.
cold,565,3,short,Are you well?
cold,451,36,long,There's a particular kind of silence that settles over a library in the late aft
cold,437,10,medium,The kitchen is on the left side of the hallway.
cold,428,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,528,2,short,Yes, exactly.
cold,476,28,long,If you could pick any single place in the world to visit next weekend, and money
cold,499,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
cold,462,8,medium,I think the weather is quite nice today.
cold,676,3,short,I'm not sure.
cold,448,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
cold,479,8,medium,I think the weather is quite nice today.
cold,478,3,short,Are you well?
cold,434,3,short,I'm not sure.
cold,463,11,medium,There's a small park about three blocks away from the apartment.
cold,468,36,long,There's a particular kind of silence that settles over a library in the late aft
cold,452,3,short,Tell me more.
cold,478,2,short,Sounds good.
cold,481,3,short,Are you well?
cold,462,11,medium,There's a small park about three blocks away from the apartment.
cold,441,28,long,If you could pick any single place in the world to visit next weekend, and money
cold,504,2,short,Yes, exactly.
cold,463,2,short,Yes, exactly.
cold,466,2,short,Yes, exactly.
cold,731,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,494,3,short,Tell me more.
cold,401,28,long,If you could pick any single place in the world to visit next weekend, and money
cold,454,3,short,I'm not sure.
cold,461,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,470,3,short,Tell me more.
cold,454,3,short,Hello there friend.
cold,480,11,medium,There's a small park about three blocks away from the apartment.
cold,453,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
cold,435,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,459,3,short,Are you well?
cold,879,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,470,3,short,Hello there friend.
cold,482,11,medium,There's a small park about three blocks away from the apartment.
cold,573,10,medium,The kitchen is on the left side of the hallway.
cold,559,28,long,If you could pick any single place in the world to visit next weekend, and money
cold,465,9,medium,Have you read that book I mentioned yesterday afternoon?
cold,532,10,medium,The kitchen is on the left side of the hallway.
cold,474,2,short,Sounds good.
cold,510,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,508,3,short,Tell me more.
cold,501,11,medium,There's a small park about three blocks away from the apartment.
cold,384,3,short,Tell me more.
cold,452,3,short,I'm not sure.
cold,527,11,medium,Could you please pass the salt and pepper across the table?
cold,458,8,medium,I think the weather is quite nice today.
cold,465,2,short,Yes, exactly.
cold,449,2,short,Sounds good.
cold,519,3,short,Tell me more.
cold,590,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
cold,678,3,short,I'm not sure.
cold,514,3,short,Hello there friend.
cold,486,3,short,Are you well?
cold,460,3,short,I'm not sure.
cold,399,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
cold,522,2,short,Yes, exactly.
cold,472,11,medium,I was hoping we could grab dinner together later this evening.
cold,491,11,medium,There's a small park about three blocks away from the apartment.
cold,439,10,medium,The kitchen is on the left side of the hallway.
cold,529,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,527,28,long,If you could pick any single place in the world to visit next weekend, and money
cold,454,3,short,Tell me more.
cold,444,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
cold,448,3,short,Tell me more.
cold,429,11,medium,I was hoping we could grab dinner together later this evening.
cold,489,11,medium,There's a small park about three blocks away from the apartment.
cold,469,11,medium,It rained heavily for most of the afternoon, then cleared up.
cold,410,3,short,Hello there friend.
cold,485,10,medium,The kitchen is on the left side of the hallway.
cold,543,2,short,Sounds good.
cold,459,11,medium,It rained heavily for most of the afternoon, then cleared up.
cold,451,36,long,There's a particular kind of silence that settles over a library in the late aft
cold,525,3,short,I'm not sure.
cold,428,11,medium,It rained heavily for most of the afternoon, then cleared up.
cold,382,9,medium,Have you read that book I mentioned yesterday afternoon?
cold,405,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
cold,548,10,medium,The kitchen is on the left side of the hallway.
cold,482,2,short,Yes, exactly.
cold,449,3,short,I'm not sure.
cold,486,11,medium,There's a small park about three blocks away from the apartment.
cold,426,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
cold,451,2,short,Sounds good.
cold,446,28,long,If you could pick any single place in the world to visit next weekend, and money
cold,493,10,medium,The kitchen is on the left side of the hallway.
cold,438,4,short,Hmm, let me think.
cold,485,3,short,Tell me more.
cold,504,11,medium,There's a small park about three blocks away from the apartment.
cold,467,3,short,Are you well?
cold,524,8,medium,I think the weather is quite nice today.
cold,505,3,short,Are you well?
cold,460,11,medium,I was hoping we could grab dinner together later this evening.
cold,410,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
cold,521,4,short,Hmm, let me think.
cold,451,8,medium,I think the weather is quite nice today.
cold,444,4,short,Hmm, let me think.
cold,430,11,medium,Could you please pass the salt and pepper across the table?
cold,437,2,short,Yes, exactly.
cold,513,8,medium,I think the weather is quite nice today.
cold,466,3,short,Are you well?
cold,483,9,medium,Have you read that book I mentioned yesterday afternoon?
cold,469,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
cold,449,8,medium,I think the weather is quite nice today.
cold,446,11,medium,There's a small park about three blocks away from the apartment.
cold,450,11,medium,There's a small park about three blocks away from the apartment.
cold,418,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
cold,389,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
cold,463,11,medium,There's a small park about three blocks away from the apartment.
cold,427,11,medium,Could you please pass the salt and pepper across the table?
cold,496,9,medium,Have you read that book I mentioned yesterday afternoon?
cold,439,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,505,8,medium,I think the weather is quite nice today.
cold,488,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,420,9,medium,Have you read that book I mentioned yesterday afternoon?
cold,494,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,509,9,medium,Have you read that book I mentioned yesterday afternoon?
cold,453,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,551,8,medium,I think the weather is quite nice today.
cold,425,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,452,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,399,9,medium,Have you read that book I mentioned yesterday afternoon?
cold,440,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,443,8,medium,I think the weather is quite nice today.
cold,789,11,medium,There's a small park about three blocks away from the apartment.
cold,438,3,short,Hello there friend.
cold,438,36,long,There's a particular kind of silence that settles over a library in the late aft
cold,405,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
cold,433,11,medium,I was hoping we could grab dinner together later this evening.
cold,462,3,short,Hello there friend.
cold,403,3,short,Tell me more.
cold,456,11,medium,Could you please pass the salt and pepper across the table?
cold,382,10,medium,The kitchen is on the left side of the hallway.
cold,627,3,short,Tell me more.
cold,422,8,medium,I think the weather is quite nice today.
cold,454,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,428,11,medium,I was hoping we could grab dinner together later this evening.
cold,442,11,medium,Could you please pass the salt and pepper across the table?
cold,563,28,long,If you could pick any single place in the world to visit next weekend, and money
cold,474,8,medium,I think the weather is quite nice today.
cold,473,3,short,I'm not sure.
cold,457,28,long,If you could pick any single place in the world to visit next weekend, and money
cold,448,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,432,11,medium,I was hoping we could grab dinner together later this evening.
cold,475,2,short,Sounds good.
cold,521,11,medium,I was hoping we could grab dinner together later this evening.
cold,440,2,short,Sounds good.
cold,433,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
cold,410,11,medium,It rained heavily for most of the afternoon, then cleared up.
cold,477,36,long,There's a particular kind of silence that settles over a library in the late aft
cold,397,3,short,Are you well?
cold,545,11,medium,I was hoping we could grab dinner together later this evening.
cold,539,9,medium,Have you read that book I mentioned yesterday afternoon?
cold,454,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,629,11,medium,Could you please pass the salt and pepper across the table?
cold,500,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,586,8,medium,I think the weather is quite nice today.
cold,513,3,short,Tell me more.
cold,451,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
cold,430,3,short,I'm not sure.
cold,463,2,short,Sounds good.
cold,388,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
cold,447,3,short,Hello there friend.
cold,578,11,medium,There's a small park about three blocks away from the apartment.
cold,467,8,medium,I think the weather is quite nice today.
cold,517,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,501,3,short,Hello there friend.
cold,510,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
cold,481,36,long,There's a particular kind of silence that settles over a library in the late aft
cold,450,8,medium,I think the weather is quite nice today.
cold,566,4,short,Hmm, let me think.
cold,469,8,medium,I think the weather is quite nice today.
cold,437,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
cold,400,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,519,11,medium,Could you please pass the salt and pepper across the table?
cold,486,2,short,Sounds good.
cold,526,8,medium,I think the weather is quite nice today.
cold,425,3,short,Hello there friend.
cold,438,3,short,Tell me more.
cold,483,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,573,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,563,3,short,I'm not sure.
cold,419,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,452,10,medium,The kitchen is on the left side of the hallway.
cold,560,11,medium,It rained heavily for most of the afternoon, then cleared up.
cold,467,3,short,Hello there friend.
cold,462,36,long,There's a particular kind of silence that settles over a library in the late aft
cold,441,11,medium,I was hoping we could grab dinner together later this evening.
cold,492,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,596,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,467,3,short,Are you well?
cold,875,11,medium,There's a small park about three blocks away from the apartment.
cold,466,11,medium,I was hoping we could grab dinner together later this evening.
cold,436,9,medium,Have you read that book I mentioned yesterday afternoon?
cold,458,11,medium,There's a small park about three blocks away from the apartment.
cold,432,11,medium,Could you please pass the salt and pepper across the table?
cold,457,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
cold,435,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,474,36,long,There's a particular kind of silence that settles over a library in the late aft
cold,457,3,short,I'm not sure.
cold,477,3,short,Hello there friend.
cold,464,2,short,Yes, exactly.
cold,437,11,medium,It rained heavily for most of the afternoon, then cleared up.
cold,441,2,short,Sounds good.
cold,392,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,435,9,medium,Have you read that book I mentioned yesterday afternoon?
warm,238,11,medium,There's a small park about three blocks away from the apartment.
warm,232,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
warm,228,8,medium,I think the weather is quite nice today.
warm,213,2,short,Sounds good.
warm,199,3,short,Hello there friend.
warm,181,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,255,4,short,Hmm, let me think.
warm,344,10,medium,The kitchen is on the left side of the hallway.
warm,220,3,short,Hello there friend.
warm,249,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,231,28,long,If you could pick any single place in the world to visit next weekend, and money
warm,184,2,short,Sounds good.
warm,189,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,238,4,short,Hmm, let me think.
warm,222,3,short,Tell me more.
warm,189,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,202,3,short,Are you well?
warm,263,3,short,Are you well?
warm,213,3,short,I'm not sure.
warm,256,3,short,Tell me more.
warm,254,3,short,Tell me more.
warm,199,3,short,Tell me more.
warm,208,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,321,10,medium,The kitchen is on the left side of the hallway.
warm,228,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,296,10,medium,The kitchen is on the left side of the hallway.
warm,216,3,short,Tell me more.
warm,160,11,medium,Could you please pass the salt and pepper across the table?
warm,257,8,medium,I think the weather is quite nice today.
warm,241,3,short,Hello there friend.
warm,224,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,200,4,short,Hmm, let me think.
warm,220,11,medium,There's a small park about three blocks away from the apartment.
warm,257,11,medium,It rained heavily for most of the afternoon, then cleared up.
warm,309,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,220,3,short,I'm not sure.
warm,273,11,medium,It rained heavily for most of the afternoon, then cleared up.
warm,240,8,medium,I think the weather is quite nice today.
warm,289,4,short,Hmm, let me think.
warm,337,11,medium,I was hoping we could grab dinner together later this evening.
warm,436,3,short,Hello there friend.
warm,196,3,short,Hello there friend.
warm,253,10,medium,The kitchen is on the left side of the hallway.
warm,206,11,medium,It rained heavily for most of the afternoon, then cleared up.
warm,191,11,medium,It rained heavily for most of the afternoon, then cleared up.
warm,191,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,213,3,short,Are you well?
warm,222,3,short,Hello there friend.
warm,226,3,short,Tell me more.
warm,260,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,339,4,short,Hmm, let me think.
warm,280,11,medium,Could you please pass the salt and pepper across the table?
warm,250,10,medium,The kitchen is on the left side of the hallway.
warm,191,3,short,I'm not sure.
warm,195,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,248,4,short,Hmm, let me think.
warm,241,3,short,Tell me more.
warm,213,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,219,9,medium,Have you read that book I mentioned yesterday afternoon?
warm,288,4,short,Hmm, let me think.
warm,192,2,short,Yes, exactly.
warm,278,2,short,Yes, exactly.
warm,229,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,231,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,216,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,216,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,249,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,238,8,medium,I think the weather is quite nice today.
warm,167,11,medium,It rained heavily for most of the afternoon, then cleared up.
warm,267,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,255,3,short,I'm not sure.
warm,239,10,medium,The kitchen is on the left side of the hallway.
warm,186,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,288,4,short,Hmm, let me think.
warm,234,11,medium,There's a small park about three blocks away from the apartment.
warm,209,8,medium,I think the weather is quite nice today.
warm,169,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,197,10,medium,The kitchen is on the left side of the hallway.
warm,256,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,229,3,short,I'm not sure.
warm,185,28,long,If you could pick any single place in the world to visit next weekend, and money
warm,202,8,medium,I think the weather is quite nice today.
warm,214,28,long,If you could pick any single place in the world to visit next weekend, and money
warm,187,3,short,Are you well?
warm,203,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
warm,248,9,medium,Have you read that book I mentioned yesterday afternoon?
warm,246,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
warm,240,11,medium,I was hoping we could grab dinner together later this evening.
warm,193,11,medium,I was hoping we could grab dinner together later this evening.
warm,278,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
warm,223,2,short,Yes, exactly.
warm,259,2,short,Sounds good.
warm,190,9,medium,Have you read that book I mentioned yesterday afternoon?
warm,241,11,medium,It rained heavily for most of the afternoon, then cleared up.
warm,290,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,241,8,medium,I think the weather is quite nice today.
warm,267,11,medium,There's a small park about three blocks away from the apartment.
warm,231,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,227,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
warm,190,10,medium,The kitchen is on the left side of the hallway.
warm,224,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,250,4,short,Hmm, let me think.
warm,260,2,short,Yes, exactly.
warm,241,11,medium,I was hoping we could grab dinner together later this evening.
warm,221,8,medium,I think the weather is quite nice today.
warm,233,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,237,9,medium,Have you read that book I mentioned yesterday afternoon?
warm,291,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,244,11,medium,It rained heavily for most of the afternoon, then cleared up.
warm,219,10,medium,The kitchen is on the left side of the hallway.
warm,239,8,medium,I think the weather is quite nice today.
warm,228,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,321,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
warm,211,3,short,I'm not sure.
warm,287,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,246,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,274,11,medium,It rained heavily for most of the afternoon, then cleared up.
warm,218,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,227,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,226,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,307,11,medium,I was hoping we could grab dinner together later this evening.
warm,218,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,309,28,long,If you could pick any single place in the world to visit next weekend, and money
warm,245,11,medium,Could you please pass the salt and pepper across the table?
warm,195,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
warm,310,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,278,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,212,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,203,11,medium,There's a small park about three blocks away from the apartment.
warm,260,9,medium,Have you read that book I mentioned yesterday afternoon?
warm,269,11,medium,There's a small park about three blocks away from the apartment.
warm,203,11,medium,It rained heavily for most of the afternoon, then cleared up.
warm,165,11,medium,There's a small park about three blocks away from the apartment.
warm,164,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,198,8,medium,I think the weather is quite nice today.
warm,229,2,short,Sounds good.
warm,238,3,short,I'm not sure.
warm,209,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,228,28,long,If you could pick any single place in the world to visit next weekend, and money
warm,262,28,long,If you could pick any single place in the world to visit next weekend, and money
warm,225,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,367,11,medium,Could you please pass the salt and pepper across the table?
warm,243,10,medium,The kitchen is on the left side of the hallway.
warm,203,11,medium,Could you please pass the salt and pepper across the table?
warm,355,28,long,If you could pick any single place in the world to visit next weekend, and money
warm,286,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,246,11,medium,There's a small park about three blocks away from the apartment.
warm,174,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
warm,168,3,short,I'm not sure.
warm,230,9,medium,Have you read that book I mentioned yesterday afternoon?
warm,188,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,232,11,medium,It rained heavily for most of the afternoon, then cleared up.
warm,206,10,medium,The kitchen is on the left side of the hallway.
warm,192,3,short,Tell me more.
warm,219,28,long,If you could pick any single place in the world to visit next weekend, and money
warm,171,3,short,Are you well?
warm,165,3,short,Are you well?
warm,168,2,short,Sounds good.
warm,190,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,244,11,medium,It rained heavily for most of the afternoon, then cleared up.
warm,164,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,176,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,177,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
warm,250,28,long,If you could pick any single place in the world to visit next weekend, and money
warm,254,3,short,Tell me more.
warm,202,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
warm,233,3,short,I'm not sure.
warm,231,11,medium,There's a small park about three blocks away from the apartment.
warm,250,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,230,3,short,Hello there friend.
warm,266,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,396,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,204,28,long,If you could pick any single place in the world to visit next weekend, and money
warm,214,2,short,Yes, exactly.
warm,247,3,short,Are you well?
warm,226,9,medium,Have you read that book I mentioned yesterday afternoon?
warm,185,2,short,Sounds good.
warm,183,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
warm,195,2,short,Sounds good.
warm,202,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,242,4,short,Hmm, let me think.
warm,285,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,221,11,medium,Could you please pass the salt and pepper across the table?
warm,273,2,short,Yes, exactly.
warm,232,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,222,9,medium,Have you read that book I mentioned yesterday afternoon?
warm,225,4,short,Hmm, let me think.
warm,211,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,211,3,short,Are you well?
warm,204,2,short,Yes, exactly.
warm,217,11,medium,There's a small park about three blocks away from the apartment.
warm,197,4,short,Hmm, let me think.
warm,218,3,short,Are you well?
warm,232,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,260,11,medium,Could you please pass the salt and pepper across the table?
warm,243,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,197,9,medium,Have you read that book I mentioned yesterday afternoon?
warm,256,8,medium,I think the weather is quite nice today.
warm,247,10,medium,The kitchen is on the left side of the hallway.
warm,233,8,medium,I think the weather is quite nice today.
warm,234,11,medium,I was hoping we could grab dinner together later this evening.
warm,207,2,short,Yes, exactly.
warm,254,10,medium,The kitchen is on the left side of the hallway.
warm,245,4,short,Hmm, let me think.
warm,191,3,short,Tell me more.
warm,203,10,medium,The kitchen is on the left side of the hallway.
warm,234,9,medium,Have you read that book I mentioned yesterday afternoon?
warm,265,3,short,Are you well?
warm,249,2,short,Yes, exactly.
warm,277,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
warm,238,3,short,Tell me more.
warm,249,2,short,Yes, exactly.
warm,283,2,short,Yes, exactly.
warm,213,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,240,11,medium,Could you please pass the salt and pepper across the table?
warm,239,11,medium,Could you please pass the salt and pepper across the table?
warm,322,28,long,If you could pick any single place in the world to visit next weekend, and money
warm,226,4,short,Hmm, let me think.
warm,219,11,medium,There's a small park about three blocks away from the apartment.
warm,229,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,173,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,196,11,medium,It rained heavily for most of the afternoon, then cleared up.
warm,217,11,medium,I was hoping we could grab dinner together later this evening.
warm,182,3,short,Tell me more.
warm,221,10,medium,The kitchen is on the left side of the hallway.
warm,191,8,medium,I think the weather is quite nice today.
warm,241,4,short,Hmm, let me think.
warm,248,11,medium,There's a small park about three blocks away from the apartment.
warm,281,8,medium,I think the weather is quite nice today.
warm,214,3,short,Tell me more.
warm,277,11,medium,I was hoping we could grab dinner together later this evening.
warm,179,2,short,Sounds good.
warm,201,11,medium,It rained heavily for most of the afternoon, then cleared up.
warm,249,11,medium,There's a small park about three blocks away from the apartment.
warm,210,8,medium,I think the weather is quite nice today.
warm,181,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,246,3,short,Tell me more.
warm,241,4,short,Hmm, let me think.
warm,341,3,short,Hello there friend.
warm,280,11,medium,There's a small park about three blocks away from the apartment.
warm,213,4,short,Hmm, let me think.
warm,221,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,273,3,short,Hello there friend.
warm,261,3,short,I'm not sure.
warm,253,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,213,11,medium,There's a small park about three blocks away from the apartment.
warm,239,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,309,3,short,Hello there friend.
warm,214,28,long,If you could pick any single place in the world to visit next weekend, and money
warm,235,28,long,If you could pick any single place in the world to visit next weekend, and money
```
