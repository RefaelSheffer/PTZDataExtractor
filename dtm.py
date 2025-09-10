# dtm.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# GeoTIFF DTM reader/sampler

from dataclasses import dataclass
from typing import Tuple, Optional
import numpy as np

# Try to import rasterio and keep the real import error for better diagnostics
try:
    import rasterio
    from rasterio.transform import rowcol, xy
    _RASTERIO_IMPORT_ERROR = None
except Exception as e:
    rasterio = None
    rowcol = None
    xy = None
    _RASTERIO_IMPORT_ERROR = repr(e)

@dataclass
class DTMInfo:
    crs_epsg: Optional[int]
    width: int
    height: int
    bounds: Tuple[float,float,float,float]  # left, bottom, right, top

class DTM:
    def __init__(self, path: str):
        if rasterio is None:
            raise RuntimeError(
                f"rasterio import failed: {_RASTERIO_IMPORT_ERROR}.\n\n"
                "Fix: make sure you install rasterio in the SAME Python environment running the app.\n"
                "Example:  python -m pip install rasterio"
            )
        self.path = path
        self.ds = rasterio.open(path, 'r')
        self.band = 1
        self.nodata = self.ds.nodata
        self.transform = self.ds.transform
        self.crs = self.ds.crs
        self.info = DTMInfo(
            crs_epsg = self.crs.to_epsg() if self.crs else None,
            width = self.ds.width,
            height = self.ds.height,
            bounds = self.ds.bounds  # left, bottom, right, top
        )

    def close(self):
        try:
            self.ds.close()
        except Exception:
            pass

    def contains(self, x: float, y: float) -> bool:
        l, b, r, t = self.info.bounds
        return (x >= l and x <= r and y >= b and y <= t)

    def sample(self, x: float, y: float) -> Optional[float]:
        """Return elevation (meters) at projected coord (x,y), or None if out of bounds / NoData."""
        if not self.contains(x, y):
            return None
        try:
            # Fast path: rasterio.sample works in dataset CRS coordinates
            val = next(self.ds.sample([(x, y)]))[0]
            if val is None or (self.nodata is not None and np.isclose(val, self.nodata)):
                return None
            return float(val)
        except Exception:
            # Fallback: index-based read
            try:
                row, col = rowcol(self.transform, x, y, op=round)
                val = self.ds.read(self.band)[row, col]
                if self.nodata is not None and np.isclose(val, self.nodata):
                    return None
                return float(val)
            except Exception:
                return None
