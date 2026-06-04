# Story 6.5 — Gemini Live-API TTFB gate

Generated: `2026-06-04T08:44:45.487089+00:00` -> `2026-06-04T08:55:42.414446+00:00`

## ✅ GATE PASS — cold p50 = 836 ms ≤ 1100 ms (re-baselined no-regression gate)

Gemini Live-API TTFB on the realistic runtime path (fresh session per turn) is at or below Cartesia's real production TTFB, so it does not regress perceived latency — and Story 6.2 cached openers mask the remainder. **Story 6.5 proceeds to Task 3** (build `GeminiClient` on the Live API with the verbatim system_instruction).

## Run metadata

- live_model: `gemini-3.1-flash-live-preview`
- voice_name: `Fenrir`
- style_prompt: `A mischievous, deadpan 16 year old with total unearned confidence — cheeky and a…`
- cold samples requested: `50` (successful: `50`, errors: `0`)
- warm samples requested: `50` (successful: `32`, errors: `0`)
- gate (re-baselined): cold p50 ≤ `1100 ms` (≈ Cartesia production p50)
- narration: verbatim system_instruction (model speaks the input exactly, no conversational reply)
- dev host: `OneHumanCompany / Linux 6.17.0-29-generic / python 3.12.3`
- google-genai SDK: `2.7.0`

## TTFB statistics (by mode)

All values in milliseconds. Gate is on **cold** p50 (the runtime opens a fresh Live session per turn) vs the re-baselined 1100 ms.

| group | n | mean +/- stdev | p25 | p50 | p75 | p90 | p95 | p99 | min | max |
|---|---|---|---|---|---|---|---|---|---|---|
| cold (fresh session/turn) | 50 | 1063 +/- 571 | 761 | 836 | 978 | 1735 | 1958 | 3189 | 696 | 3313 |
| warm (session reused) | 32 | 725 +/- 100 | 658 | 742 | 800 | 823 | 850 | 914 | 485 | 933 |

## Methodology

- Single-clock per-request delta: `t_send` after `send_client_content(turn_complete=True)`, `t_first` on the first `inline_data.data` audio chunk; `ttfb_ms = (t_first - t_send) // 1e6`. Matches the production `tts.first_frame.ttfb_ms` log shape.
- Cold: fresh `client.aio.live.connect()` per request (runtime path).
- Warm: one session reused across the phase; each turn drained to `turn_complete` before the next send.
- Inter-request sleep: 200 ms (conservative for the undocumented preview-tier rate limit).
- Transcript pool: shared with the Story 6.1 Cartesia spike (7 short ≤5 words, 7 medium ≤15, 6 long) for a like-for-like comparison.
- Cartesia baseline for reference: production p50 ~1067 ms (DR-001 SSE); warm-WS spike best-case ~230 ms.
- Gemini output is 24 kHz mono s16le; this spike measures arrival timing only (no resample — that lands in Task 3's `GeminiClient`).

## Raw samples

`mode,ttfb_ms,word_count,bucket,transcript` (cold first, then warm).

```
cold,3060,3,short,I'm not sure.
cold,834,11,medium,I was hoping we could grab dinner together later this evening.
cold,987,36,long,There's a particular kind of silence that settles over a library in the late aft
cold,815,3,short,Are you well?
cold,952,36,long,There's a particular kind of silence that settles over a library in the late aft
cold,859,3,short,Are you well?
cold,707,2,short,Yes, exactly.
cold,840,3,short,Tell me more.
cold,749,3,short,Tell me more.
cold,874,36,long,There's a particular kind of silence that settles over a library in the late aft
cold,837,8,medium,I think the weather is quite nice today.
cold,817,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
cold,867,2,short,Sounds good.
cold,719,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,996,36,long,There's a particular kind of silence that settles over a library in the late aft
cold,830,11,medium,It rained heavily for most of the afternoon, then cleared up.
cold,762,10,medium,The kitchen is on the left side of the hallway.
cold,1902,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,722,3,short,Are you well?
cold,920,11,medium,I was hoping we could grab dinner together later this evening.
cold,1041,10,medium,The kitchen is on the left side of the hallway.
cold,1637,3,short,Tell me more.
cold,721,39,long,I find that the best way to remember a new word is to use it in a sentence about
cold,724,36,long,There's a particular kind of silence that settles over a library in the late aft
cold,812,36,long,There's a particular kind of silence that settles over a library in the late aft
cold,3313,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,1959,28,long,If you could pick any single place in the world to visit next weekend, and money
cold,1630,4,short,Hmm, let me think.
cold,1672,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,803,2,short,Sounds good.
cold,1711,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
cold,858,10,medium,The kitchen is on the left side of the hallway.
cold,1956,8,medium,I think the weather is quite nice today.
cold,836,9,medium,Have you read that book I mentioned yesterday afternoon?
cold,696,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
cold,737,2,short,Sounds good.
cold,723,4,short,Hmm, let me think.
cold,934,3,short,I'm not sure.
cold,797,3,short,Hello there friend.
cold,761,2,short,Sounds good.
cold,859,8,medium,I think the weather is quite nice today.
cold,1716,28,long,If you could pick any single place in the world to visit next weekend, and money
cold,861,3,short,Hello there friend.
cold,705,9,medium,Have you read that book I mentioned yesterday afternoon?
cold,778,11,medium,I was hoping we could grab dinner together later this evening.
cold,723,9,medium,Have you read that book I mentioned yesterday afternoon?
cold,853,2,short,Sounds good.
cold,787,4,short,Hmm, let me think.
cold,737,3,short,Hello there friend.
cold,778,2,short,Sounds good.
warm,485,9,medium,Have you read that book I mentioned yesterday afternoon?
warm,644,11,medium,I was hoping we could grab dinner together later this evening.
warm,686,4,short,Hmm, let me think.
warm,588,3,short,Are you well?
warm,657,2,short,Sounds good.
warm,602,10,medium,The kitchen is on the left side of the hallway.
warm,543,4,short,Hmm, let me think.
warm,657,9,medium,Have you read that book I mentioned yesterday afternoon?
warm,778,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,775,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
warm,705,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,799,8,medium,I think the weather is quite nice today.
warm,599,11,medium,Could you please pass the salt and pepper across the table?
warm,814,2,short,Sounds good.
warm,774,23,long,When I look outside the window in the morning, the trees seem to glow with a sof
warm,660,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,672,3,short,Tell me more.
warm,714,39,long,I find that the best way to remember a new word is to use it in a sentence about
warm,751,11,medium,I was hoping we could grab dinner together later this evening.
warm,789,11,medium,Could you please pass the salt and pepper across the table?
warm,702,4,short,Hmm, let me think.
warm,933,3,short,Hello there friend.
warm,659,3,short,Hello there friend.
warm,810,19,long,The pipeline measures latency from end-of-speech to first audio byte, and report
warm,772,8,medium,I think the weather is quite nice today.
warm,802,9,medium,Have you read that book I mentioned yesterday afternoon?
warm,751,11,medium,It rained heavily for most of the afternoon, then cleared up.
warm,833,3,short,Hello there friend.
warm,823,36,long,There's a particular kind of silence that settles over a library in the late aft
warm,819,37,long,Walking through the city early on a Sunday morning, before the cafes open and be
warm,733,28,long,If you could pick any single place in the world to visit next weekend, and money
warm,871,11,medium,It rained heavily for most of the afternoon, then cleared up.
```
