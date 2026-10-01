"""Utilidades para manipulação de imagem (crop, debug, salvamento)."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import cv2
import numpy as np


def crop_roi(
    image: np.ndarray,
    x_pct: float,
    y_pct: float,
    w_pct: float,
    h_pct: float,
) -> np.ndarray:
    """Recorta uma região da imagem usando coordenadas percentuais (0.0–1.0).

    Parameters
    ----------
    image : np.ndarray
        Imagem fonte (grayscale ou color).
    x_pct, y_pct : float
        Canto superior esquerdo em fração da largura/altura.
    w_pct, h_pct : float
        Largura e altura da ROI em fração.

    Returns
    -------
    np.ndarray
        Sub-imagem recortada.
    """
    h, w = image.shape[:2]
    x1 = int(w * x_pct)
    y1 = int(h * y_pct)
    x2 = int(w * (x_pct + w_pct))
    y2 = int(h * (y_pct + h_pct))

    # Clampar aos limites da imagem
    x1, x2 = max(0, x1), min(w, x2)
    y1, y2 = max(0, y1), min(h, y2)

    return image[y1:y2, x1:x2].copy()


def draw_rois_debug(
    image: np.ndarray,
    rois: Sequence[tuple[str, float, float, float, float, str]],
) -> np.ndarray:
    """Desenha retângulos coloridos com labels na imagem para depuração.

    Parameters
    ----------
    image : np.ndarray
        Imagem original (será copiada, não modificada in-place).
    rois : sequence of (name, x_pct, y_pct, w_pct, h_pct, roi_type)
        Lista de ROIs com coordenadas percentuais.

    Returns
    -------
    np.ndarray
        Imagem com retângulos e labels desenhados.
    """
    canvas = image.copy()
    if len(canvas.shape) == 2:
        canvas = cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)

    h, w = canvas.shape[:2]

    # Cores por tipo de ROI
    color_map = {
        "text": (0, 255, 0),       # verde
        "table": (255, 0, 0),      # azul
        "skip": (0, 0, 255),       # vermelho
        "comments": (255, 255, 0), # ciano
        "footnotes": (0, 165, 255),  # laranja
    }

    for name, x_pct, y_pct, w_pct, h_pct, roi_type in rois:
        x1 = int(w * x_pct)
        y1 = int(h * y_pct)
        x2 = int(w * (x_pct + w_pct))
        y2 = int(h * (y_pct + h_pct))

        color = color_map.get(roi_type, (200, 200, 200))
        thickness = 2
        cv2.rectangle(canvas, (x1, y1), (x2, y2), color, thickness)

        # Label
        font_scale = 0.5
        cv2.putText(
            canvas,
            f"{name} ({roi_type})",
            (x1 + 4, y1 + 16),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            color,
            1,
            cv2.LINE_AA,
        )

    return canvas


def save_debug_image(image: np.ndarray, path: Path) -> None:
    """Salva imagem no disco, criando diretórios se necessário."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), image)
