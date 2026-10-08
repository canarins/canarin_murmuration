# Murmuration

Canarin's pollution world-model service. It consumes Birdhouse's fan-out stream (off
the ingestion hot path), maintains a trust-weighted model of the pollution field, and
upserts six result relations into the Canarin database every ~5 minutes: forecasts,
virtual sensors, anomaly scores, device trust, exposure and position — each row with
a credibility score.

Design references: *A Self-Correcting World Model for Urban Air-Quality Fields*
(Drive › Canarin World Model, §5 "System Implementation" matches this repo) and
`canarin-world-model-design.md` v0.3.

## How it fits the platform

```
Pico / Femto ──UDP──▶ Birdhouse ──▶ Data.data_points_canonical (RDS MySQL 8.4)
                         │
                         └──XADD──▶ birdhouse:fanout (ElastiCache Redis stream)
                                        │
                                        ▼  consumer group "murmuration"
                                   Murmuration (ECS Fargate, 1 task)
                                   ├─ state: Redis hashes  murm:trust / murm:exposure / murm:ontime
                                   ├─ weights: S3 (immutable, `current` pointer)
                                   └─ writes: Murmuration.* (public) + MurmurationInternal.* (admin)
                                                  │
                                                  ▼
                                        Plumage dashboard · Chirping alerts
```

Vocabulary: a **measure** is one quantity a device reports; a **channel** is one declared stream of a measure on one device, `WebFront.pollutants` (per device:
`pollutant_type_id`, `instance_index`, `scale_factor`) and a measure type is
`WebFront.pollutant_types.id` (1 PM1, 2 PM2.5, 3 PM10, 13 TVOC, 17 humidity,
18 pressure, 16 temperature = device self-heat, 863 battery). What the model does
with each type is a row in `Murmuration.measure_registry` (block, role, decoder head,
sanity window, nested-fraction parent). Device position comes from
`Devices.devices_hardware.last_lat/last_long/gps_hdop`; what the platform does not
hold yet — device type (Pico/Femto), power source, revision — is declared in
`Murmuration.device_profile` (optional; unknown devices default to `other`).

**Privacy rule, enforced by schema.** A public prediction names a measure at a
place and a time, never its source. `Murmuration.field_forecast` and
`Murmuration.virtual_sensor` carry no `device_id` (there is a test). The four
device-level relations live in `MurmurationInternal` — grant that schema to admins only.

**Event time ≠ arrival time.** Every stream entry carries the device's bucketed `ts`
and Birdhouse's `received_at`. Readings are routed fresh / late / stale / skew; a
buffer flushed after an outage is *late*: stored for training and marked for
reanalysis, but it never moves live state or trust (there is a test for that too).

## What v0.1 is

The **kriging encoder + reliability controller**, wired end to end. Kriging is the
permanent encoder and bootstrap fallback agreed for Murmuration; the learned
recurrent core (RSSM) plugs in behind the same interfaces in a later version.

| Paper component | v0.1 | Module |
|---|---|---|
| Event-time routing | ✅ | `routing.py` |
| Device-local gates: sanity, flatline, PM1 ≤ PM2.5 ≤ PM10 with neighbours-only attribution | ✅ | `gates.py` |
| Encoder: trust × exposure × position-confidence weighted assimilation | ✅ kriging | `field.py` |
| Dynamics: anomaly rolled forward, bands widen with horizon, 1-parameter recalibration | ✅ AR(1) | `field.py` |
| Controller C: asymmetric EMA, floor, hard gate, forced listening, per-device calibration | ✅ | `trust.py`, `cycle.py` |
| Hierarchical cold-start priors (type → model → instance) | ✅ | `trust.py` |
| Exposure: seeded from type/power source, daily coupling update | ✅ | `devices.py` |
| Position: uncertainty-first priors (Δp correction later) | ✅ | `devices.py` |
| Credibility per row, timeliness-based fleet size, 14-day / 4-device floors | ✅ | `credibility.py` |
| Learned RSSM core, GNN encoder, neural-field decoder | ⏳ v0.2+ | — |
| Weather (Thermal) as the control input | ⏳ Plumage's `Ambient` service is the natural source | — |

## Local stack

```bash
docker compose up -d mysql redis
pip install -e ".[dev]"
murmuration migrate                          # dev stub of Devices/WebFront/Data + Murmuration schemas
murmuration simulate --cycles 300 --days 21  # synthetic Paris fleet, 25 h of live cycles
python scripts/evaluate_sim.py               # trust, verdicts, late data, forecast skill
python -m pytest                             # 31 tests: unit + integration (needs MySQL + Redis)
```

`scripts/reset_local_db.sh` drops the local schemas, flushes Redis and the artifacts.
Env vars default to `127.0.0.1`, user `plumage`/`plumage`, Redis without TLS.

Reference run (14 Picos + 2 Femtos, one stuck, one drifting, one dark for 6 h then
flushing its buffer; PM2.5 at the cells holding healthy Picos):

| h | RMSE (µg/m³) | persistence | skill | 90% coverage |
|---|---|---|---|---|
| 1 | 2.20 | 3.99 | +44.8% | 0.96 |
| 2 | 2.45 | 5.48 | +55.3% | 0.95 |
| 3 | 2.62 | 6.52 | +59.8% | 0.95 |
| 6 | 2.93 | 6.66 | +56.0% | 0.95 |

Stuck sensor hard-gated, drifting sensor at the trust floor, healthy sensors 0.53–0.97,
the dark device's trust untouched by its late flush. Synthetic data only.

## Commands

| Command | What it does |
|---|---|
| `murmuration migrate [--outputs-only]` | Apply `db/migrations` — **always `--outputs-only` against the real RDS** |
| `murmuration train --type 2 3 --days 30` | Fit field params + per-device residual calibration → new immutable artifact |
| `murmuration cycle` | One nowcast cycle now |
| `murmuration run --interval 300` | The long-running Fargate task |
| `murmuration rollback VERSION` | Point `current` at an older artifact |

Env: `DB_HOST DB_PORT DB_USER DB_PASS DB_SSL_CA` · `REDIS_HOST REDIS_PORT REDIS_TLS REDIS_AUTH_TOKEN`
· `BIRDHOUSE_FANOUT_STREAM` · `MURM_ARTIFACTS` (`file://…` | `s3://bucket/prefix/`) · `MURM_STATE_PREFIX`.

## Deploying on Canarin AWS

Same pattern as Birdhouse: CloudFormation for the static resources, CLI for the
first task definition + service, then `.github/workflows/deploy.yml` (digest-pinned
image, clone-the-live-task-def) on every merge to `main`.

1. **Birdhouse fan-out** — merge `canarins/Canarins_birdhouse#36` (adds the stream).
2. **Infra** (`deploy/cfn-snippets.yml` → `canarin_infra`): `MurmurationSG` (egress only)
   + ingress rules on the RDS and ElastiCache SGs, ECR repo `canarin-murmuration`,
   log group `/ecs/canarin-murmuration`, S3 bucket `canarin-murmuration-artifacts`
   + S3 rights on `canarin-ecs-app-task-role`.
3. **Database** — `deploy/mysql-user.sql` (user `murmuration_app`, read on platform
   schemas, all on its own two; grant `Murmuration.*` to `plumage_app`), password in
   Secrets Manager `canarin/db-murmuration-app-prod`; then
   `murmuration migrate --outputs-only` once from anywhere inside the VPC.
4. **Profiles** — insert the fleet into `Murmuration.device_profile`
   (Picos 9/11/12/15/73/74: `pico`,`mains`,`gnss`; Femto+ 72: `femto` + its power source).
5. **Service** — push a first image tagged `bootstrap`, then `MURM_SG=sg-… deploy/create-service.sh`.
   Set `AWS_ROLE_ARN` on the repo and CI takes over.
6. **First model** — `murmuration train --type 2 3 --days 30` (as a one-off Fargate task
   or from a bastion). Until it exists the service autofits hourly from history; under
   14 days / 4 devices every row is `provisional`.

Rollback: `murmuration rollback <version>` for the model, `workflow_dispatch` with an
older `image_tag` for the service, `BIRDHOUSE_FANOUT=false` on Birdhouse to stop the stream.

## Known limits

- Single instance per channel (`instance_index = 1`); the BME280/680 duplicate-sensor gate is not wired.
- Credibility constants are first-order fits to the paper's Table 16; recalibrate on real data.
- Never trained on operational readings — the first job after deployment.
