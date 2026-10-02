"""Locate complete table rectangles before OCR, with full-page fallback."""
from __future__ import annotations

import cv2
import numpy as np

from .table_geometry import TableExtraction, extract_table


def find_table_boxes(img: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Find outlined table candidates; headers/content validate each candidate.

    Unlike the old y=44% crop this includes headers above that position and
    accommodates different GE print settings. Duplicate inner/outer contours
    are collapsed. Header, trend and graph boxes are not accepted by shape alone.
    """
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape
    # GE has dark frames; Hologic uses white grid lines on a gray background.
    dark = cv2.threshold(gray, 120, 255, cv2.THRESH_BINARY_INV)[1]
    light = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)[1]
    contours = []
    for mask in (dark, light):
        found, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        contours.extend(found)
    boxes = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if not (.25*width < w < .98*width and .045*height < h < .4*height):
            continue
        # Closed table frame, not the bounding box of disconnected text.
        if cv2.contourArea(contour) < .85*w*h:
            continue
        if any(abs(x-a)+abs(y-b)+abs(w-c)+abs(h-d) < .02*width for a,b,c,d in boxes):
            continue
        boxes.append((x,y,w,h))
    return sorted(boxes, key=lambda b:b[1])


def read_positioned_table(img: np.ndarray, engine, lang: str = "") -> TableExtraction:
    results = []
    for x,y,w,h in find_table_boxes(img):
        inset = max(2, round(img.shape[1]/700))
        crop = img[y+inset:y+h-inset, x+inset:x+w-inset]
        gray = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        scale = max(1.0, 2400/gray.shape[1])
        # Preserve thin minus signs: no opening/erosion on numerical cells.
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        result = extract_table(engine.recognize_tokens(gray, lang=lang, psm=6))
        if result.text:
            results.append(result)
    if len(results) == 1:
        return results[0]
    if len(results) > 1:
        return TableExtraction(warnings=["Mais de uma tabela de medição na página; revisão necessária"])
    # Borderless reports (including Hologic): use the whole page so the
    # manufacturer color heuristic cannot cut off the relevant headers.
    scale = max(1.0, 2200/img.shape[1])
    page = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return extract_table(engine.recognize_tokens(page, lang=lang, psm=11))
