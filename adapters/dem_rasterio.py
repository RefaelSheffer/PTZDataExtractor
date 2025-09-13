"""Adapter implementing :class:`~core.i2g_core.DemSampler` using rasterio.

This small wrapper keeps rasterio imports confined to an adapter layer
so that the core logic can remain free of heavy dependencies.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

try:  # pragma: no cover - import errors are handled at runtime
    import rasterio
except Exception as e:  # pragma: no cover
    rasterio = None  # type: ignore
    _RASTERIO_ERROR = repr(e)
else:
    _RASTERIO_ERROR = None

try:  # pragma: no cover - pyproj optional
    from pyproj import Geod
except Exception:  # pragma: no cover
    Geod = None  # type: ignore

from core.i2g_core import DemSampler


class RasterioDemSampler(DemSampler):
    """Sample elevations from a GeoTIFF using rasterio."""

    def __init__(self, path: str):
        if rasterio is None:
            raise RuntimeError(
                "rasterio import failed: %s" % (_RASTERIO_ERROR,),
            )
        self._ds = rasterio.open(path, "r")
        self._band = 1
        self._nodata = self._ds.nodata
        self._transform = self._ds.transform
        self.meters_per_unit = 1.0
        try:
            crs = self._ds.crs
            if crs and crs.is_geographic:
                bounds = self._ds.bounds
                lon = (bounds.left + bounds.right) / 2.0
                lat = (bounds.bottom + bounds.top) / 2.0
                if Geod is not None:
                    geod = Geod(ellps="WGS84")
                    _, _, dx = geod.inv(lon, lat, lon + 1.0, lat)
                    _, _, dy = geod.inv(lon, lat, lon, lat + 1.0)
                    self.meters_per_unit = (abs(dx) + abs(dy)) / 2.0
                else:  # pragma: no cover - fallback
                    self.meters_per_unit = 111_320.0
        except Exception:  # pragma: no cover - defensive
            self.meters_per_unit = 1.0

    def close(self) -> None:
        try:
            self._ds.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def elevation(self, x: float, y: float) -> Optional[float]:
        """Return elevation at ``(x, y)`` or ``None`` when unavailable."""
        try:
            val = next(self._ds.sample([(x, y)]))[0]
            if val is None or (self._nodata is not None and np.isclose(val, self._nodata)):
                return None
            return float(val)
        except Exception:
            return None
