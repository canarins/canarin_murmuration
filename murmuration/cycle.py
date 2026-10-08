"""One Murmuration nowcast cycle (paper §4 inference loop, §5.3).

  1. drain the fan-out, route each record by event time
  2. device-local gates (sanity, flatline, nested fractions) -> hard gates
  3. encode: weight each fresh reading by trust x exposure x position-confidence
  4. neighbours-only twin for every reading -> residual, anomaly score
  5. update slow state: trust (asymmetric EMA, floor, forced listening),
     on-time fraction, and — daily — exposure from device<->field coupling
  6. assimilate, roll forward, decode on the output grid and virtual sites
  7. idempotent upserts of the six relations, each row with credibility
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from . import credibility as cred
from .config import Settings
from .devices import (DeviceInfo, exposure_class, exposure_prior, position_confidence,
                      position_prior_m, update_exposure)
from .field import Assimilated, FieldParams, fit_params, forecast, loo_expectation, nowcast
from .gates import attribute, flatline, nested_violations, sanity
from .geo import cell_centre, grid_cells, to_km
from .routing import Route, Router
from .trust import (TrustParams, TrustState, agreement, assimilation_weight, hard_gate,
                    is_probe_cycle, pooled_prior, update)

log = logging.getLogger("murmuration")

ONTIME_RATE = 0.05
CHECKPOINT_EVERY = 12          # slow state to Plumage ~hourly
SLOW_UPDATE_EVERY = 288        # exposure re-estimate ~daily
AGREE_K = 1.5                  # agreement scale: mean agreement of a healthy device ≈ 0.83


class Murmuration:
    def __init__(self, settings: Settings, plumage, queue, state, artifacts=None):
        self.s = settings
        self.db = plumage
        self.q = queue
        self.state = state
        self.artifacts = artifacts
        self.router = Router(settings.fresh_window_s, settings.reanalysis_horizon_s,
                             settings.skew_tolerance_s)
        self.tp = TrustParams(settings.eta_gain, settings.eta_loss, settings.trust_floor,
                              settings.probe_every_cycles)
        self.model_version = settings.model_version
        self._params: dict[str, FieldParams] = {}
        self._autofit_at: dict[str, datetime] = {}
        self._resid: dict[str, dict[str, tuple[float, float]]] = {}
        self._load_artifact()

    # ------------------------------------------------------------------ model --
    def _load_artifact(self):
        if self.artifacts is None:
            return
        cur = self.artifacts.load_current()
        if cur:
            self.model_version, payload = cur
            self._params = {c: FieldParams.from_dict(p) for c, p in payload["codes"].items()}
            self._resid = {c: {d: tuple(v) for d, v in m.items()}
                           for c, m in payload.get("residual", {}).items()}
            log.info("loaded model %s (%d codes)", self.model_version, len(self._params))

    def params_for(self, code: str, devs: list[str], centre, now: datetime) -> FieldParams:
        """Trained artifact if present; otherwise fit on recent history (refit hourly)."""
        if code in self._params and code not in self._autofit_at:
            return self._params[code]
        last = self._autofit_at.get(code)
        if last is not None and (now - last) < timedelta(hours=1):
            return self._params[code]
        S, hours, _ = self.db.hourly_history(code, now - timedelta(days=self.s.history_days), now, devs)
        pos = self._pos_km(devs, centre)
        ok = np.isfinite(S).sum() >= 48
        p = fit_params(S, hours, pos, self.s.length_scale_km, val_frac=0.0)[0] if ok else FieldParams(
            length_km=self.s.length_scale_km)
        if not ok and np.isfinite(S).any():
            p.clim_hour = [float(np.nanmean(S))] * 24
            p.s2 = float(np.nanvar(S)) or 1.0
        self._params[code] = p
        self._autofit_at[code] = now
        return p

    # ---------------------------------------------------------------- helpers --
    def _pos_km(self, devs, centre):
        lat0, lon0 = centre
        return to_km([self.devices[d].lat for d in devs], [self.devices[d].lon for d in devs], lat0, lon0)

    def _exposure(self, d: DeviceInfo) -> tuple[float, bool]:
        prior, cls = exposure_prior(d)
        personal = cls == "personal"
        if personal:
            return 0.0, True
        v = self.state.get_exposure(d.device_id)
        return (prior if v is None else v), False

    def _trust(self, dev: str, code: str, prior: float) -> TrustState:
        s = self.state.get_trust(dev, code)
        return s if s is not None else TrustState(trust=prior)

    # ------------------------------------------------------------------ cycle --
    def run_cycle(self, now: datetime | None = None) -> dict:
        t_start = time.time()
        now = now or datetime.now(timezone.utc)
        cycle = self.state.next_cycle()
        self.devices = self.db.devices()
        registry = self.db.registry()

        # 1. drain + route --------------------------------------------------
        records = self.q.drain()
        counts = defaultdict(int)
        late = defaultdict(int)
        fresh = defaultdict(list)                      # (dev, code) -> [values]
        for r in records:
            route = self.router.route(r.ts, now)
            counts[route.value] += 1
            if route is Route.LATE:
                late[(r.code, r.ts.replace(minute=0, second=0, microsecond=0))] += 1
            if route is not Route.FRESH or r.device_id not in self.devices or r.instance_index != 0:
                continue
            fresh[(r.device_id, r.code)].append(r.value)
        self.db.mark_late(late)

        if not self.devices:
            self._log(now, t_start, counts, 0, "no enrolled devices")
            return {"cycle": cycle, **counts, "codes": {}}

        lat0 = float(np.mean([d.lat for d in self.devices.values()]))
        lon0 = float(np.mean([d.lon for d in self.devices.values()]))
        centre = (lat0, lon0)

        # 2a. sanity gate ------------------------------------------------------
        gated: dict[tuple[str, str], str] = {}
        values: dict[tuple[str, str], float] = {}
        for (d, c), vs in fresh.items():
            v = float(np.mean(vs))
            spec = registry.get(c)
            if spec is not None and not sanity(v, spec):
                gated[(d, c)] = "sanity"
            values[(d, c)] = v

        targets = sorted({c for (_, c) in values if c in registry and registry[c].decoder_head})

        # 3–4. encode + neighbours-only twins, per target code -----------------
        per_code = {}
        for code in targets:
            devs = sorted(d for (d, c) in values if c == code)
            hist_devs = sorted(self.devices)
            S24, _, _ = self.db.hourly_history(code, now - timedelta(hours=24), now, hist_devs)
            active = [d for j, d in enumerate(hist_devs) if np.isfinite(S24[:, j]).any() or d in devs]
            for j, d in enumerate(hist_devs):
                if d in devs and flatline(S24[:, j], min_points=6):
                    gated.setdefault((d, code), "flatline")

            p = self.params_for(code, hist_devs, centre, now)
            pos = self._pos_km(devs, centre)
            anom = np.array([values[(d, code)] for d in devs]) - p.clim_hour[now.hour]

            all_tr = self.state.all_trust()
            by_model = defaultdict(list)
            for (dd, cc), ts_ in all_tr.items():
                if cc == code and dd in self.devices and ts_.n_updates >= 50:
                    by_model[self.devices[dd].device_model or self.devices[dd].device_type].append(ts_.trust)
            fleet_default = registry[code].cold_start_trust
            priors = pooled_prior(by_model, fleet_default)

            trust, expo, personal, probing, w = [], [], [], [], []
            for d in devs:
                info = self.devices[d]
                tr = self._trust(d, code, priors.get(info.device_model or info.device_type, fleet_default))
                e, pers = self._exposure(info)
                pr = is_probe_cycle(d, code, cycle, self.tp) and tr.trust < 0.5
                unc, _ = position_prior_m(info)
                pc = position_confidence(unc, p.length_km)
                g = (d, code) in gated
                trust.append(tr); expo.append(e); personal.append(pers); probing.append(pr)
                w.append(0.0 if g else assimilation_weight(tr, pr) * e * pc)
            w = np.array(w)
            mu, sig, nn = loo_expectation(pos, anom, w, p, self.s.neighbour_radius_km) \
                if len(devs) else (np.zeros(0),) * 3
            # judge against each device's calibrated (bias, spread) where training provided one
            cal = self._resid.get(code, {})
            bias = np.array([cal.get(d, (0.0, 0.0))[0] for d in devs])
            spread = np.array([max(float(sig[i]), cal.get(d, (0.0, 0.0))[1]) for i, d in enumerate(devs)]) \
                if len(devs) else np.zeros(0)
            sig = AGREE_K * spread
            mu = mu + bias
            z = (anom - mu) / np.maximum(sig, 1e-9) if len(devs) else np.zeros(0)
            per_code[code] = dict(devs=devs, pos=pos, anom=anom, trust=trust, expo=expo,
                                  personal=personal, probing=probing, w=w, mu=mu, sig=sig, nn=nn,
                                  z=z, params=p, active=active)

        # 2b. nested-fraction gate with neighbours-only attribution ------------
        by_dev = defaultdict(dict)
        for (d, c), v in values.items():
            by_dev[d][c] = v
        zlookup = {(d, c): float(pc["z"][i]) for c, pc in per_code.items() for i, d in enumerate(pc["devs"])}
        for d, rec in by_dev.items():
            for child, parent in nested_violations(rec, registry):
                culprit = attribute(zlookup.get((d, child), 0.0), zlookup.get((d, parent), 0.0), child, parent)
                gated.setdefault((d, culprit), "nested")

        # 5–7. trust update, assimilate, decode, write -------------------------
        hist_days = {c: self.db.history_days(c, now) for c in per_code}
        sites = self.db.virtual_sites()
        cells = grid_cells([d.lat for d in self.devices.values()], [d.lon for d in self.devices.values()],
                           self.s.grid_step_deg, self.s.grid_margin_deg)
        centres = [cell_centre(c, self.s.grid_step_deg) for c in cells]
        cell_km = to_km([c[0] for c in centres], [c[1] for c in centres], lat0, lon0)
        checkpoint = cycle % CHECKPOINT_EVERY == 0

        fc_rows, vs_rows, an_rows, tr_rows = [], [], [], []
        summary = {}
        fresh_devs = {d for (d, _) in values}
        for code, pc in per_code.items():
            devs = pc["devs"]
            n_gated = 0
            for i, d in enumerate(devs):
                st = pc["trust"][i]
                if (d, code) in gated:
                    st = hard_gate(st, self.tp); n_gated += 1
                    verdict = f"gate:{gated[(d, code)]}"
                elif pc["personal"][i] or pc["nn"][i] < self.s.min_neighbours_for_trust:
                    verdict = "unjudged"          # personal exposure or too sparse: no update
                else:
                    a = agreement(float(pc["anom"][i] - pc["mu"][i]), float(pc["sig"][i]))
                    st = update(st, a, self.tp)
                    zz = abs(float(pc["z"][i]))
                    verdict = "healthy" if zz < 3 else ("fault" if st.trust < 0.5 else "transient")
                self.state.set_trust(d, code, st)
                pc["trust"][i] = st
                an_rows.append((self.model_version, d, 0, code, now, float(pc["anom"][i] + pc["params"].clim_hour[now.hour]),
                                float(pc["mu"][i] + pc["params"].clim_hour[now.hour]), float(pc["z"][i]), verdict,
                                0.0))
                if checkpoint:
                    tr_rows.append([self.model_version, d, code, now, st.trust, st.hard_fault, 0.0])

            # on-time fraction (timeliness, not enrolment)
            ambient_active = [d for d in pc["active"] if not exposure_prior(self.devices[d])[1] == "personal"]
            eff = 0.0
            for d in ambient_active:
                prev = self.state.get_ontime(d)
                prev = 0.5 if prev is None else prev
                cur = prev + ONTIME_RATE * ((1.0 if d in devs else 0.0) - prev)
                self.state.set_ontime(d, cur)
                eff += cur

            ok = np.array([(d, code) not in gated and pc["w"][i] > 0 for i, d in enumerate(devs)], bool)
            w_final = np.array([0.0 if not ok[i] else
                                assimilation_weight(pc["trust"][i], pc["probing"][i]) * pc["expo"][i]
                                * position_confidence(position_prior_m(self.devices[d])[0], pc["params"].length_km)
                                for i, d in enumerate(devs)])
            m = w_final > 0
            post = Assimilated(pc["pos"][m], pc["anom"][m], w_final[m], pc["params"])
            contrib_trust = float(np.mean([pc["trust"][i].trust for i in np.where(m)[0]])) if m.any() else 0.0
            coverage = (m.sum() / max(1, len(ambient_active))) if ambient_active else 0.0
            sup = cred.Support(hist_days[code], eff, contrib_trust, min(1.0, coverage))
            f_score = cred.score_forecast(sup, cap=self.s.provisional_cap,
                                          min_days=self.s.min_history_days, min_devices=self.s.min_devices)
            prov = cred.provisional(sup, self.s.min_history_days, self.s.min_devices)

            fc = forecast(post, cell_km, now.hour, self.s.horizons_h)
            for h, (val, lo, hi) in fc.items():
                valid = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=h)
                hs = f_score * (0.97 ** (h - 1))    # longer leads are a little less credible
                for k, cid in enumerate(cells):
                    fc_rows.append((self.model_version, now, valid, cid, centres[k][0], centres[k][1], code,
                                    max(0.0, float(val[k])), max(0.0, float(lo[k])), float(hi[k]),
                                    hs, cred.band(hs), prov))

            if sites:
                s_km = to_km([s[1] for s in sites], [s[2] for s in sites], lat0, lon0)
                val, lo, hi = nowcast(post, s_km, now.hour)
                dens = (np.linalg.norm(s_km[:, None] - pc["pos"][m][None], axis=-1) <= 1.0).sum(1) \
                    if m.any() else np.zeros(len(sites), int)
                for k, (sid, la, lo_) in enumerate(sites):
                    vsup = cred.Support(hist_days[code], eff, contrib_trust, min(1.0, coverage), int(dens[k]))
                    vsc = cred.score_virtual(vsup, cap=self.s.provisional_cap,
                                             min_days=self.s.min_history_days, min_devices=self.s.min_devices)
                    vs_rows.append((self.model_version, now, sid, la, lo_, code, max(0.0, float(val[k])),
                                    max(0.0, float(lo[k])), float(hi[k]), vsc, cred.band(vsc)))

            # per-device state credibility = history-weighted forecast score
            an_rows = [r[:-1] + (f_score,) if r[3] == code else r for r in an_rows]
            tr_rows = [r[:-1] + [f_score] if r[2] == code else r for r in tr_rows]
            summary[code] = dict(devices=len(devs), assimilated=int(m.sum()), gated=n_gated,
                                 effective_fleet=round(eff, 2), credibility=round(f_score, 3),
                                 provisional=prov, history_days=round(hist_days[code], 1))

        # daily slow update: exposure from device<->field coupling
        if cycle % SLOW_UPDATE_EVERY == 0 and per_code:
            self._slow_exposure(now, centre, next(iter(sorted(per_code))))

        exp_rows, pos_rows = [], []
        if checkpoint:
            base_cred = max((v["credibility"] for v in summary.values()), default=0.0)
            for d, info in self.devices.items():
                e, pers = self._exposure(info)
                exp_rows.append((self.model_version, d, now, e, exposure_class(e, pers), base_cred))
                unc, method = position_prior_m(info)
                pos_rows.append((self.model_version, d, info.lat, info.lon, unc, method, now, base_cred))

        self.db.write_forecasts(fc_rows)
        self.db.write_virtual(vs_rows)
        self.db.write_anomalies(an_rows)
        self.db.write_trust([tuple(r) for r in tr_rows])
        self.db.write_exposure(exp_rows)
        self.db.write_position(pos_rows)
        self._log(now, t_start, counts, len(gated), None)
        return {"cycle": cycle, **counts, "gated": len(gated), "fresh_devices": len(fresh_devs),
                "codes": summary}

    def _slow_exposure(self, now, centre, code):
        devs = sorted(self.devices)
        S, hours, _ = self.db.hourly_history(code, now - timedelta(days=7), now, devs)
        p = self._params.get(code)
        if p is None or S.shape[0] < 72:
            return
        A = S - np.array(p.clim_hour)[hours][:, None]
        pos = self._pos_km(devs, centre)
        outdoor = np.array([exposure_prior(self.devices[d])[0] >= 0.7 for d in devs])
        for j, d in enumerate(devs):
            info = self.devices[d]
            if exposure_prior(info)[1] == "personal":
                continue
            nb = outdoor.copy(); nb[j] = False
            if nb.sum() < 2:
                continue
            field = np.full(S.shape[0], np.nan)
            for t in range(S.shape[0]):
                ok = nb & np.isfinite(A[t])
                if ok.sum() >= 2:
                    post = Assimilated(pos[ok], A[t, ok], np.ones(ok.sum()), p)
                    field[t] = post.predict(pos[j:j + 1])[0][0]
            prev, _ = self._exposure(info)
            self.state.set_exposure(d, update_exposure(prev, A[:, j], field))

    def _log(self, now, t_start, counts, n_gated, note):
        self.db.log_cycle(model_version=self.model_version, started_at=now,
                          finished_at=datetime.now(timezone.utc),
                          n_fresh=counts.get("fresh", 0), n_late=counts.get("late", 0),
                          n_stale=counts.get("stale", 0), n_skew=counts.get("skew", 0),
                          n_gated=n_gated, model_used="kriging-baseline",
                          note=note or f"{time.time() - t_start:.2f}s")
