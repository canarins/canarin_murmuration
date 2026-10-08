"""Geometry helpers: local km projection, distances, output grid cells."""
from __future__ import annotations

import math

import numpy as np

KM_PER_DEG_LAT = 110.57


def km_per_deg_lon(lat: float) -> float:
    return 111.32 * math.cos(math.radians(lat))


def to_km(lat, lon, lat0: float, lon0: float) -> np.ndarray:
    """Equirectangular projection to km around (lat0, lon0). Returns [..., 2] (x, y)."""
    lat = np.asarray(lat, float)
    lon = np.asarray(lon, float)
    x = (lon - lon0) * km_per_deg_lon(lat0)
    y = (lat - lat0) * KM_PER_DEG_LAT
    return np.stack([x, y], axis=-1)


def pairwise_km(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Distances between point sets a [n,2] and b [m,2] already in km."""
    return np.linalg.norm(a[:, None, :] - b[None, :, :], axis=-1)


def cell_id(lat: float, lon: float, step: float) -> int:
    """Stable integer id of the grid cell containing (lat, lon)."""
    row = int(math.floor((lat + 90.0) / step))
    col = int(math.floor((lon + 180.0) / step))
    return row * 1_000_000 + col


def cell_centre(cid: int, step: float) -> tuple[float, float]:
    row, col = divmod(cid, 1_000_000)
    return (row + 0.5) * step - 90.0, (col + 0.5) * step - 180.0


def grid_cells(lats, lons, step: float, margin: float) -> list[int]:
    """All cells covering the fleet bounding box plus a margin."""
    lo_lat, hi_lat = min(lats) - margin, max(lats) + margin
    lo_lon, hi_lon = min(lons) - margin, max(lons) + margin
    cells = []
    lat = lo_lat
    while lat <= hi_lat + 1e-12:
        lon = lo_lon
        while lon <= hi_lon + 1e-12:
            cells.append(cell_id(lat, lon, step))
            lon += step
        lat += step
    return sorted(set(cells))
