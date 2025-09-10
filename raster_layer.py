# raster_layer.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from typing import Tuple
import numpy as np

try:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import Affine, xy
    _RASTERIO_IMPORT_ERROR = None
except Exception as e:
    rasterio = None
    Resampling = None
    Affine = None
    xy = None
    _RASTERIO_IMPORT_ERROR = repr(e)

class RasterLayer:
    """Loads a GeoTIFF as a downsampled image and keeps mapping to CRS coordinates."""
    def __init__(self, path: str, max_size: int = 2048):
        if rasterio is None:
            raise RuntimeError(f"rasterio required: {_RASTERIO_IMPORT_ERROR}. Install: pip install rasterio")
        self.path = path
        self.ds = rasterio.open(path, "r")
        self.crs = self.ds.crs
        self.transform = self.ds.transform  # full-res affine
        self.bounds = self.ds.bounds
        self.size = (self.ds.width, self.ds.height)
        self.rgb = None
        self.over_transform = None  # transform for the downsampled image
        self._read_overview(max_size)

    def _read_overview(self, max_size: int):
        W, H = self.ds.width, self.ds.height
        scale = max(W, H) / float(max_size) if max(W, H) > max_size else 1.0
        out_w, out_h = int(round(W/scale)), int(round(H/scale))
        data = self.ds.read(out_shape=(self.ds.count, out_h, out_w), resampling=Resampling.bilinear)
        if self.ds.count >= 3:
            arr = np.stack([data[0], data[1], data[2]], axis=2).astype(np.float32)
            arr = np.clip(arr, 0, 255).astype(np.uint8)
        else:
            band = data[0].astype(np.float32)
            m0, m1 = np.nanpercentile(band, [2, 98])
            band = np.clip((band - m0)/(m1-m0+1e-6)*255.0, 0, 255).astype(np.uint8)
            arr = band
        self.rgb = arr
        # overview transform = original transform scaled by factor
        sx = W / float(out_w); sy = H / float(out_h)
        self.over_transform = self.transform * Affine.scale(sx, sy)

    def downsampled_image(self) -> np.ndarray:
        return self.rgb

    # scene (pix in downsampled image) -> CRS (X,Y)
    def scene_to_geo(self, x_scene: float, y_scene: float) -> Tuple[float, float]:
        """Map scene pixel coords -> CRS coordinates (X,Y) using over_transform."""
        x, y = xy(self.over_transform, y_scene, x_scene)  # (row=y, col=x)
        return float(x), float(y)

    # CRS (X,Y) -> scene (pix in downsampled image)
    def geo_to_scene(self, X: float, Y: float) -> Tuple[float, float]:
        """Map CRS coordinates (X,Y) -> scene pixel coords (x_scene,y_scene)."""
        if self.over_transform is None:
            raise RuntimeError("RasterLayer.over_transform is None (overview not initialized)")
        inv = ~self.over_transform  # Affine inverse
        x_scene, y_scene = inv * (X, Y)  # returns (col=x, row=y)
        return float(x_scene), float(y_scene)

    def close(self):
        try:
            self.ds.close()
        except Exception:
            pass
