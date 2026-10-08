"""Field estimator — the kriging encoder that Murmuration keeps permanently
(decision: Oracle's kriging is the always-on encoder and bootstrap fallback,
not a throwaway MVP).

Model, per normalized_code:
  y_i(t) = clim_i(hour) + a(x_i, t) + eps_i
  a(., t) ~ zero-mean GP, cov s2 * exp(-d / L)
  a(x, t+h) = phi^h * a(x, t) + innovation      (AR(1) anomaly persistence)
  Var(eps_i) = tau2 / w_i,   w_i = trust * exposure * position_confidence

So trust enters exactly where the paper puts it: as the per-device
observation-error covariance in assimilation. A distrusted device is not
deleted; its nugget grows and it pulls the field less.

Expected readings for trust/anomaly scoring are computed leave-one-out,
from neighbours only: an expectation that uses the device's own history
absorbs a persistent fault and attribution collapses (paper §6.12).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .geo import pairwise_km


@dataclass
class FieldParams:
    """Fitted per code by `murmuration train`; stored in the model artifact."""
    s2: float = 1.0            # anomaly variance
    tau2: float = 0.1          # base observation noise variance
    length_km: float = 1.5
    phi: float = 0.8           # lag-1 hourly anomaly autocorrelation
    ci_scale: float = 1.0      # one-parameter recalibration (paper §6.7–6.8)
    clim_hour: list[float] = field(default_factory=lambda: [0.0] * 24)  # fleet climatology

    def to_dict(self) -> dict:
        return dict(s2=self.s2, tau2=self.tau2, length_km=self.length_km, phi=self.phi,
                    ci_scale=self.ci_scale, clim_hour=list(self.clim_hour))

    @classmethod
    def from_dict(cls, d: dict) -> "FieldParams":
        return cls(**d)


@dataclass
class Assimilated:
    """Posterior anomaly field given one batch of weighted observations."""
    pos_km: np.ndarray        # [n,2] observing devices
    anom: np.ndarray          # [n] observed anomalies
    weights: np.ndarray       # [n] w_i in (0,1]
    params: FieldParams
    _alpha: np.ndarray | None = None
    _Kinv: np.ndarray | None = None

    def __post_init__(self):
        n = len(self.anom)
        if n == 0:
            return
        K = self._cov(pairwise_km(self.pos_km, self.pos_km))
        nugget = self.params.tau2 / np.maximum(self.weights, 1e-6)
        K = K + np.diag(nugget)
        self._Kinv = np.linalg.inv(K)
        self._alpha = self._Kinv @ self.anom

    def _cov(self, d: np.ndarray) -> np.ndarray:
        return self.params.s2 * np.exp(-d / self.params.length_km)

    def predict(self, q_km: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Anomaly mean and variance at query points [m,2] (km)."""
        m = len(q_km)
        if self._alpha is None:
            return np.zeros(m), np.full(m, self.params.s2)
        k = self._cov(pairwise_km(q_km, self.pos_km))      # [m,n]
        mean = k @ self._alpha
        var = self.params.s2 - np.einsum("ij,jk,ik->i", k, self._Kinv, k)
        return mean, np.maximum(var, 1e-9)


def loo_expectation(pos_km: np.ndarray, anom: np.ndarray, weights: np.ndarray,
                    params: FieldParams, radius_km: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Neighbours-only expected anomaly for each device.

    Returns (mean [n], sigma [n] incl. the device's own base noise, n_neighbours [n]).
    """
    n = len(anom)
    mean = np.zeros(n)
    sig = np.full(n, np.sqrt(params.s2 + params.tau2))
    nn = np.zeros(n, int)
    D = pairwise_km(pos_km, pos_km)
    for i in range(n):
        mask = (np.arange(n) != i) & (D[i] <= radius_km) & (weights > 0)
        nn[i] = int(mask.sum())
        if nn[i] == 0:
            continue
        post = Assimilated(pos_km[mask], anom[mask], weights[mask], params)
        mu, var = post.predict(pos_km[i:i + 1])
        mean[i] = mu[0]
        sig[i] = float(np.sqrt(var[0] + params.tau2))
    return mean, sig, nn


def forecast(post: Assimilated, q_km: np.ndarray, base_now_hour: int, horizons: tuple[int, ...],
             z90: float = 1.645) -> dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Roll the anomaly field forward under AR(1) and add target-hour climatology.

    Uncertainty compounds with horizon: Var(t+h) = phi^2h Var_post + s2 (1 - phi^2h).
    Returns {h: (value, ci_low, ci_high)} at the query points (90% band).
    """
    p = post.params
    a0, v0 = post.predict(q_km)
    out = {}
    for h in horizons:
        ph = p.phi ** h
        mean_anom = ph * a0
        var = (ph ** 2) * v0 + p.s2 * (1.0 - ph ** 2)
        base = p.clim_hour[(base_now_hour + h) % 24]
        val = base + mean_anom
        half = z90 * p.ci_scale * np.sqrt(var)
        out[h] = (val, val - half, val + half)
    return out


def nowcast(post: Assimilated, q_km: np.ndarray, hour: int, z90: float = 1.645):
    p = post.params
    a, v = post.predict(q_km)
    val = p.clim_hour[hour % 24] + a
    half = z90 * p.ci_scale * np.sqrt(v)
    return val, val - half, val + half


def residual_scales(series: np.ndarray, hours: np.ndarray, pos_km: np.ndarray, neighbour_mask: np.ndarray,
                    params: FieldParams, radius_km: float, max_samples: int = 200) -> np.ndarray:
    """Per-device (bias, robust spread) of the neighbours-only residual over history.

    The bias is a first-order per-location embedding ("whatever is special
    here", design §4A): a device in a canyon pocket reads persistently above its
    neighbours. Frozen at training time, so a fault that develops later still
    shows up as a residual instead of being absorbed.

    This is the controller's warm-up calibration (paper §6.3): a device sitting in
    a real micro-feature (a canyon pocket, near a source) disagrees with its
    neighbours *by construction*; judging it against a fleet-wide sigma would
    amputate a healthy sensor. Robust (MAD) so a fault late in the training
    window does not inflate the scale much. NaN where it cannot be estimated.
    """
    T, N = series.shape
    anom = series - np.array(params.clim_hour)[hours][:, None]
    ts = np.linspace(0, T - 1, min(T, max_samples)).astype(int)
    res = [[] for _ in range(N)]
    D = pairwise_km(pos_km, pos_km)
    for t in ts:
        obs = anom[t]
        ok = np.isfinite(obs)
        for i in range(N):
            if not ok[i]:
                continue
            nb = ok & neighbour_mask & (np.arange(N) != i) & (D[i] <= radius_km)
            if nb.sum() < 2:
                continue
            post = Assimilated(pos_km[nb], obs[nb], np.ones(nb.sum()), params)
            res[i].append(obs[i] - post.predict(pos_km[i:i + 1])[0][0])
    bias = np.full(N, np.nan)
    scale = np.full(N, np.nan)
    for i, r in enumerate(res):
        if len(r) >= 24:
            r = np.asarray(r)
            bias[i] = float(np.median(r))
            scale[i] = 1.4826 * float(np.median(np.abs(r - bias[i])))
    return bias, scale


# --------------------------------------------------------------------------- #
# fitting (offline, `murmuration train`)
# --------------------------------------------------------------------------- #
def fit_params(series: np.ndarray, hours: np.ndarray, pos_km: np.ndarray,
               length_km: float = 1.5, val_frac: float = 0.2) -> tuple[FieldParams, dict]:
    """Fit climatology, s2, tau2, phi from an hourly [T, N] matrix (NaN allowed),
    then choose ci_scale on a validation slice so 90% bands cover ~90%.
    """
    T, N = series.shape
    split = int(T * (1 - val_frac))
    tr = series[:split]
    clim_dev = np.full((24, N), np.nan)
    for h in range(24):
        rows = tr[hours[:split] == h]
        if len(rows):
            clim_dev[h] = np.nanmean(rows, axis=0)
    clim_hour = np.nanmean(clim_dev, axis=1)
    clim_hour = np.where(np.isfinite(clim_hour), clim_hour, np.nanmean(tr))
    anom = series - clim_hour[hours][:, None]

    a_tr = anom[:split]
    s_total = float(np.nanvar(a_tr))
    # nugget: half the mean squared difference between near-collocated pairs ~ noise
    D = pairwise_km(pos_km, pos_km)
    iu = np.triu_indices(N, 1)
    diffs = []
    for i, j in zip(*iu):
        if D[i, j] < 0.5:
            d = a_tr[:, i] - a_tr[:, j]
            d = d[np.isfinite(d)]
            if d.size:
                diffs.append(0.5 * float(np.mean(d ** 2)))
    tau2 = float(np.median(diffs)) if diffs else 0.15 * s_total
    s2 = max(s_total - tau2, 1e-6)
    x, y = a_tr[:-1].ravel(), a_tr[1:].ravel()
    m = np.isfinite(x) & np.isfinite(y)
    phi = float(np.clip(np.corrcoef(x[m], y[m])[0, 1], 0.0, 0.99)) if m.sum() > 10 else 0.8

    params = FieldParams(s2=s2, tau2=max(tau2, 1e-6), length_km=length_km, phi=phi,
                         ci_scale=1.0, clim_hour=[float(v) for v in clim_hour])

    # validation: 1-step forecast at each device from the others (LOO), raw coverage
    zs = []
    for t in range(split, T - 1, max(1, (T - split) // 60)):
        obs = anom[t]
        ok = np.isfinite(obs)
        if ok.sum() < 3:
            continue
        for i in np.where(ok)[0]:
            others = ok.copy(); others[i] = False
            post = Assimilated(pos_km[others], obs[others], np.ones(others.sum()), params)
            fc = forecast(post, pos_km[i:i + 1], int(hours[t]), (1,), z90=1.0)
            val, lo, hi = fc[1]
            sd = float(hi[0] - val[0])
            truth = series[t + 1, i]
            if np.isfinite(truth) and sd > 0:
                zs.append((truth - val[0]) / sd)
    report = {"n_val": len(zs)}
    if len(zs) >= 20:
        zs = np.abs(np.array(zs))
        q90 = float(np.quantile(zs, 0.90))
        params.ci_scale = float(np.clip(q90 / 1.645, 0.25, 4.0))
        report.update(raw_cov90=float(np.mean(zs <= 1.645)),
                      recal_cov90=float(np.mean(zs <= 1.645 * params.ci_scale)))
    report.update(s2=params.s2, tau2=params.tau2, phi=params.phi, ci_scale=params.ci_scale)
    return params, report
