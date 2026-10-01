from __future__ import annotations

from pathlib import Path

import numpy as np
import pydicom
from PIL import Image



def _normalize_to_uint8(arr: np.ndarray) -> np.ndarray:
    if arr.dtype == np.uint8:
        return arr

    arr = arr.astype(np.float32)
    arr -= arr.min()
    max_value = arr.max()
    if max_value > 0:
        arr /= max_value
    arr *= 255.0
    return arr.astype(np.uint8)



def _apply_photometric_interpretation(ds: pydicom.Dataset, arr: np.ndarray) -> np.ndarray:
    photometric = str(getattr(ds, "PhotometricInterpretation", "")).upper()
    if photometric == "MONOCHROME1":
        return 255 - arr
    return arr



def dicom_to_pil(dicom_path: Path, resize_width: int | None = None) -> Image.Image:
    ds = pydicom.dcmread(str(dicom_path))
    if not hasattr(ds, "PixelData"):
        raise ValueError(f"Arquivo sem PixelData: {dicom_path}")

    arr = ds.pixel_array
    arr = _normalize_to_uint8(arr)
    arr = _apply_photometric_interpretation(ds, arr)

    if arr.ndim == 2:
        img = Image.fromarray(arr).convert("L")
    elif arr.ndim == 3:
        img = Image.fromarray(arr)
    else:
        raise ValueError(f"Dimensão de imagem não suportada: {arr.shape}")

    if resize_width and img.width > 0:
        resize_height = int((resize_width / img.width) * img.height)
        img = img.resize((resize_width, resize_height), Image.LANCZOS)

    return img



def save_png(img: Image.Image, output_path: Path, compress_level: int = 3) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, format="PNG", optimize=True, compress_level=compress_level)
