# Wake-word models

Custom Picovoice Porcupine wake-word model files (`.ppn`).

The pipeline loads exactly one wake-word file at startup, named in
`setup.toml` `[wakeword] model_path`. The shipped phrase is "Hey OLAF".

## Current model

| File | Phrase | Trained | Platform | Tier |
|---|---|---|---|---|
| `hey_olaf.ppn` | "Hey OLAF" | 2026-05-05 | Linux (x86_64) | Personal-use free tier |

## Retraining workflow

When Story 5.5's soak (or your own ambient sanity checks) reveals
false-positive or false-negative issues that `sensitivity` tuning can't
fix, retrain the `.ppn`:

1. Sign in to https://console.picovoice.ai/.
2. **Wake Word** → existing project → adjust phrasing or accent samples.
3. Re-export `.ppn` for the same platform listed above.
4. Replace `models/wakeword/hey_olaf.ppn`.
5. Restart the pipeline:
   - During dev: `Ctrl-C` + `just run`.
   - After Story 5.4 lands: `systemctl restart voice-agent-pipeline`.
6. Re-run the ambient FP test from Story 1.6 / 5.5.

> **Free-tier rate limit.** Picovoice's free tier allows roughly **one
> wake-word training per month** (verify in your console — the quota has
> moved over time). Don't burn it on small tweaks. Before retraining:
> exhaust `sensitivity` tuning in `setup.toml` (it's free to change),
> review your accent / sample inputs in the console (they save between
> runs), and only hit "Train" once you're confident the new config will
> ship.

## Platform-specific `.ppn` files

Each `.ppn` is built for a specific platform — a Linux x86_64 file will
**not** load on macOS, Windows, or Raspberry Pi (Porcupine refuses with
a platform-mismatch error at `pvporcupine.create`).

Currently committed: **Linux (x86_64)** only. Other platforms need their
own training run. Options as the project grows:

- **Multi-platform commit:** ship `hey_olaf.linux-x86_64.ppn`,
  `hey_olaf.macos-arm64.ppn`, etc., and have `setup.toml` pick by
  `platform.system()` + `platform.machine()`. Adds a config dimension.
- **Per-host training:** each contributor trains their own and gitignores
  the local file. Drops the "fresh clone runs after `uv sync`" property.

V1 ships option zero (single committed file, Linux x86_64). Revisit when
a contributor on a non-Linux host hits the wall.

## Access key vs. model file — they are different

The Picovoice access key (`PICOVOICE_ACCESS_KEY` in `.env`) authenticates
the **runtime SDK**. The `.ppn` file is the trained **model**. Rotate
them independently:

- **Compromised access key** → regenerate at console.picovoice.ai → update
  `.env` only. The model file stays.
- **Bad accuracy / new phrase** → retrain the model (steps above) → swap
  the `.ppn`. The access key stays.

## Why we commit the `.ppn`

Picovoice's free tier permits redistribution of personal-use wake-word
files. Committing the file keeps the repo self-contained — a fresh clone
runs after `uv sync` + setting `PICOVOICE_ACCESS_KEY`. If we move to a
paid tier with different redistribution terms, this folder may need to
move into the host's filesystem (`/etc/voice-agent-pipeline/`) and out
of git.

