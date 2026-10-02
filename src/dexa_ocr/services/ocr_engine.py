"""Camada de abstração para engines OCR.

Suporta Tesseract (padrão) e PaddleOCR (opcional).
O restante do pipeline não depende diretamente de nenhuma engine.
"""

from __future__ import annotations

import abc
from typing import Optional

import numpy as np

from ..config import Settings
from ..utils.logger import get_logger
from .table_geometry import OCRToken

log = get_logger("ocr_engine")


# ============================================================================
# Interface abstrata
# ============================================================================

class OCREngine(abc.ABC):
    """Interface base para engines OCR."""

    def recognize_tokens(self, image: np.ndarray, *, lang: str = "", psm: int = 6) -> list[OCRToken]:
        """Read text with boxes. Engines without boxes must not guess score columns."""
        raise NotImplementedError("Engine does not provide positioned OCR tokens")

    @abc.abstractmethod
    def recognize(self, image: np.ndarray, *, psm: int = 6, lang: str = "", whitelist: str = "") -> str:
        """Executa OCR em uma imagem pré-processada.

        Parameters
        ----------
        image : np.ndarray
            Imagem (grayscale ou binarizada).
        psm : int
            Page segmentation mode (Tesseract) — ignorado por engines que não suportam.
        lang : str
            Idioma do OCR. Se vazio, usa o padrão da engine.
        whitelist : str
            Caracteres permitidos. Se vazio, aceita todos.

        Returns
        -------
        str
            Texto reconhecido.
        """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Nome da engine para metadados."""


# ============================================================================
# Tesseract
# ============================================================================

class TesseractEngine(OCREngine):
    """Wrapper para Tesseract via pytesseract."""

    def __init__(self, settings: Settings) -> None:
        import pytesseract
        if settings.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
        self._pytesseract = pytesseract
        self._default_lang = settings.tesseract_lang
        log.info("TesseractEngine inicializado (lang=%s)", self._default_lang)

    @property
    def name(self) -> str:
        return "tesseract"

    def recognize_tokens(self, image: np.ndarray, *, lang: str = "", psm: int = 6) -> list[OCRToken]:
        data = self._pytesseract.image_to_data(
            image, lang=lang or self._default_lang, config=f"--oem 3 --psm {psm}",
            output_type=self._pytesseract.Output.DICT,
        )
        return [OCRToken(
            text.strip(), float(data["left"][i]), float(data["top"][i]),
            float(data["left"][i] + data["width"][i]),
            float(data["top"][i] + data["height"][i]),
            float(data["conf"][i]) / 100.0,
        ) for i, text in enumerate(data["text"]) if text.strip()]

    def recognize(self, image: np.ndarray, *, psm: int = 6, lang: str = "", whitelist: str = "") -> str:
        lang = lang or self._default_lang
        config_parts = [f"--oem 3 --psm {psm}"]
        if whitelist:
            config_parts.append(f'-c tessedit_char_whitelist="{whitelist}"')
        config_str = " ".join(config_parts)

        text = self._pytesseract.image_to_string(image, lang=lang, config=config_str)
        return text.strip()


# ============================================================================
# PaddleOCR (opcional)
# ============================================================================

class PaddleOCREngine(OCREngine):
    """Wrapper para PaddleOCR.

    Só é instanciado se o pacote paddleocr estiver disponível.
    Caso contrário, a factory retorna TesseractEngine.
    """

    def __init__(self, settings: Settings) -> None:
        from paddleocr import PaddleOCR  # type: ignore[import-untyped]
        self._ocr = PaddleOCR(
            use_angle_cls=True,
            lang=settings.paddleocr_lang,
            show_log=False,
        )
        log.info("PaddleOCREngine inicializado (lang=%s)", settings.paddleocr_lang)

    @property
    def name(self) -> str:
        return "paddleocr"

    def recognize(self, image: np.ndarray, *, psm: int = 6, lang: str = "", whitelist: str = "") -> str:
        tokens = self.recognize_tokens(image, lang=lang, classify_orientation=True)
        return self._group_into_lines([
            (token.left, token.top, token.text) for token in tokens if token.confidence >= 0.65
        ])

    def recognize_tokens(self, image: np.ndarray, *, lang: str = "", psm: int = 6,
                         classify_orientation: bool = False) -> list[OCRToken]:
        import cv2

        # PaddleOCR requires 3-channel BGR images
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)

        try:
            # The page/table is already upright. Rotating individual numeric
            # boxes can turn -0.9 into 6'0- and corrupt a valid score.
            result = self._ocr.ocr(image, cls=classify_orientation)
        except TypeError:
            # PaddleOCR >= 3.x removed cls kwarg
            result = self._ocr.ocr(image)
        if not result or not result[0]:
            return []

        first = result[0]

        # Preserve boxes and confidence, including low-confidence placeholders.
        detections: list[OCRToken] = []

        if isinstance(first, dict):
            # v3 dict-style result
            texts = first.get("rec_texts", [])
            polys = first.get("dt_polys", [])
            scores = first.get("rec_scores", [])
            for i, (poly, text) in enumerate(zip(polys, texts)):
                if not text:
                    continue
                conf = scores[i] if i < len(scores) else 1.0
                x_min = min(pt[0] for pt in poly)
                y_min = min(pt[1] for pt in poly)
                detections.append(OCRToken(text, x_min, y_min,
                    max(pt[0] for pt in poly), max(pt[1] for pt in poly), float(conf)))
        else:
            # v2 list-style result: list of [bbox, (text, conf)]
            for line_info in first:
                bbox = line_info[0]
                text_conf = line_info[1] if line_info[1] else None
                if not text_conf:
                    continue
                text = text_conf[0]
                conf = text_conf[1] if len(text_conf) > 1 else 1.0
                if not text:
                    continue
                x_min = min(pt[0] for pt in bbox)
                y_min = min(pt[1] for pt in bbox)
                detections.append(OCRToken(text, x_min, y_min,
                    max(pt[0] for pt in bbox), max(pt[1] for pt in bbox), float(conf)))

        return detections

    @staticmethod
    def _group_into_lines(
        detections: list[tuple[float, float, str]],
        y_tolerance: float = 20.0,
    ) -> str:
        """Group text detections into lines by Y-coordinate proximity.

        Detections on the same horizontal row (within *y_tolerance* pixels)
        are merged left-to-right, separated by spaces.  Rows are then
        joined by newlines so the output looks like regular OCR text lines.
        """
        if not detections:
            return ""

        # Sort by Y first, then X
        detections.sort(key=lambda d: (d[1], d[0]))

        lines: list[list[tuple[float, str]]] = []
        current_y = detections[0][1]
        current_line: list[tuple[float, str]] = []

        for x, y, text in detections:
            if abs(y - current_y) > y_tolerance:
                # new row
                if current_line:
                    lines.append(current_line)
                current_line = [(x, text)]
                current_y = y
            else:
                current_line.append((x, text))

        if current_line:
            lines.append(current_line)

        # Within each line, sort left-to-right and join with spaces
        result_lines: list[str] = []
        for line in lines:
            line.sort(key=lambda item: item[0])
            result_lines.append(" ".join(text for _, text in line))

        return "\n".join(result_lines)


# ============================================================================
# Factory
# ============================================================================

def create_engine(settings: Settings) -> OCREngine:
    """Cria a engine OCR conforme configuração.

    Se ``ocr_engine`` for "paddleocr" e o pacote estiver disponível,
    retorna PaddleOCREngine. Caso contrário, retorna TesseractEngine.
    """
    if settings.ocr_engine == "paddleocr":
        try:
            return PaddleOCREngine(settings)
        except ImportError as exc:
            log.warning(
                "PaddleOCR solicitado mas não instalado — usando Tesseract como fallback. Motivo: %s",
                exc,
                exc_info=True,
            )

    return TesseractEngine(settings)


# ============================================================================
# Configurações por tipo de ROI
# ============================================================================

def get_ocr_params(roi_name: str, settings: Settings) -> dict:
    """Retorna parâmetros de OCR otimizados para cada tipo de ROI.

    Returns
    -------
    dict com chaves: psm, lang, whitelist
    """
    if roi_name in ("header", "patient_info"):
        # Texto corrido em português — sem whitelist restritiva
        return {"psm": 6, "lang": settings.tesseract_lang, "whitelist": ""}

    if roi_name in ("table_data",):
        # Tabela numérica — sem whitelist para evitar erros de leitura
        return {
            "psm": 6,
            "lang": settings.tesseract_lang,
            "whitelist": "",
        }

    if roi_name in ("table_header", "site_title"):
        # Cabeçalho de tabela — texto misto
        return {"psm": 6, "lang": settings.tesseract_lang, "whitelist": ""}

    if roi_name in ("comments",):
        return {"psm": 6, "lang": settings.tesseract_lang, "whitelist": ""}

    if roi_name in ("footnotes",):
        return {"psm": 6, "lang": settings.tesseract_lang, "whitelist": ""}

    # Fallback
    return {"psm": 6, "lang": settings.tesseract_lang, "whitelist": ""}
