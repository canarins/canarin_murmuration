# Murmuration

Canarin's pollution world-model service. It consumes the Birdhouse fan-out (off the
ingestion hot path), maintains a trust-weighted model of the pollution field, and
upserts six result relations into Plumage every ~5 minutes: forecasts, virtual
sensors, anomaly scores, device trust, exposure and position — each row with a
credibility score.

Design references: *A Self-Correcting World Model for Urban Air-Quality Fields*
(Drive › Canarin World Model) and `canarin-world-model-design.md` v0.3.

## What v0.1 is

The **kriging encoder + reliability controller**, wired end to end. This is the
permanent encoder and bootstrap fallback agreed for Murmuration (Oracle's kriging is
kept, not thrown away). The learned recurrent core (RSSM) plugs in behind the same
interfaces in a later version.

| Paper component | v0.1 | Module |
|---|---|---|
| Event-time routing (fresh / late / stale / skew) | ✅ | `routing.py` |
| Device-local gates: sanity, flatline, PM1 ≤ PM2.5 ≤ PM10 + neighbours-only attribution | ✅ | `gates.py` |
| Encoder: trust × exposure × position-confidence weighted assimilation | ✅ kriging | `field.py` |
| Dynamics: anomaly rolled forward, bands widen with horizon, 1-parameter recalibration | ✅ AR(1) | `field.py` |
| Controller C: asymmetric EMA, floor, hard gate, forced listening, per-device calibration | ✅ | `trust.py`, `cycle.py` |
| Hierarchical cold-start priors (type → device model → instance) | ✅ | `trust.py` |
| Exposure: seeded from type/power source, daily coupling update | ✅ | `devices.py` |
| Position: uncertainty-first priors | ✅ (Δp correction: not yet) | `devices.py` |
| Credibility per row, timeliness-based fleet size, 14-day / 4-device floors | ✅ | `credibility.py` |
| Learned RSSM core, GNN encoder, neural-field decoder | ⏳ v0.2+ | — |
| Thermal (weather service) as model input | ⏳ needs Thermal | — |

## Privacy rule (enforced by schema)

A public prediction names a pollutant at a place and a time, **never its source**.
`field_forecast` and `virtual_sensor` (schema `public`) have no `device_id` and no
contributor list — there is a test for it. Device-level relations live in
`murm_internal` and must only be exposed to admins in Plumage.

## Local stack

```bash
docker compose up -d postgres redis          # Plumage stand-in + Redis (state and fan-out)
pip install -e ".[dev]"
murmuration migrate                          # stub Plumage tables + the six output relations
murmuration simulate --cycles 300 --days 21  # synthetic Paris fleet, 25 h of live cycles
python scripts/evaluate_sim.py               # trust, verdicts, late data, forecast skill
python -m pytest                             # unit + integration (needs Postgres + Redis)
```

`scripts/reset_local_db.sh` wipes the local database, Redis and artifacts.

Reference run (14 Picos + 2 Femtos, one stuck, one drifting, one dark for 6 h):

| h | RMSE (µg/m³) | persistence | skill | 90% coverage |
|---|---|---|---|---|
| 1 | 2.21 | 3.99 | +44.6% | 0.96 |
| 2 | 2.45 | 5.48 | +55.2% | 0.95 |
| 3 | 2.62 | 6.52 | +59.8% | 0.95 |
| 6 | 2.92 | 6.66 | +56.1% | 0.95 |

Stuck sensor hard-gated, drifting sensor at trust floor, healthy sensors 0.53–0.97,
the dark device's trust untouched by its late flush. Synthetic data only; the
persistence baseline here uses the last complete hour.

## Commands

| Command | What it does |
|---|---|
| `murmuration migrate [--outputs-only]` | Apply `db/migrations` (use `--outputs-only` against real Plumage) |
| `murmuration train --code pm25 pm10 --days 30` | Fit field params + per-device residual calibration → new immutable artifact |
| `murmuration cycle` | One nowcast cycle now |
| `murmuration run --interval 300` | The long-running Fargate task |
| `murmuration rollback VERSION` | Point `current` at an older artifact |

Configuration is by environment: `MURM_PG_DSN`, `MURM_REDIS_URL`, `MURM_QUEUE`
(`redis` | `sqs`), `MURM_SQS_URL`, `MURM_ARTIFACTS` (`file://…` | `s3://bucket/prefix/`).

## Deploying to Canarin AWS (next)

1. Run `migrate --outputs-only` against Plumage (creates `murm_internal` + the two public tables).
2. Subscribe an SQS queue to the Birdhouse fan-out; message body = one normalized reading
   (`device_id, instance_index, normalized_code, ts, value`).
3. S3 bucket for artifacts; `train` as a scheduled job (daily), `run` as one ECS Fargate service.
4. ElastiCache Redis for slow state; Secrets Manager for the Plumage DSN.

## Known limits

- Single channel per field (`instance_index = 0`); duplicate-sensor gate (BME280/680) not wired yet.
- Credibility constants are first-order fits to the paper's Table 16; recalibrate on real data.
- Never trained on operational Plumage readings — that is the first job after deployment.
