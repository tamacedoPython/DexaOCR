"""Read DXA cells under explicit headers, retaining gaps between OCR boxes.

No manufacturer/column-count guess is used here. Unrecognized or ambiguous
headers produce no measurements; an unreadable cell never shifts its neighbor.
Coordinates are relative to the same page image for headers and values.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from statistics import median
import unicodedata

from .parser_table import _LUMBAR_PATTERN, _FEMUR_PATTERN, _FOREARM_PATTERN


@dataclass(frozen=True)
class OCRToken:
    text: str
    left: float
    top: float
    right: float
    bottom: float
    confidence: float = 1.0

    @property
    def x(self) -> float:
        return (self.left + self.right) / 2

    @property
    def y(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def height(self) -> float:
        return max(1.0, self.bottom - self.top)


@dataclass
class TableExtraction:
    text: str = ""
    warnings: list[str] = field(default_factory=list)


def _key(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).lower()
    return "".join(c for c in text if c.isascii() and c.isalnum())


def _header_kind(text: str) -> str | None:
    key = _key(text)
    if key in ("tscore", "escoret", "scoret"):
        return "t"
    if key in ("zscore", "escorez", "scorez"):
        return "z"
    if re.fullmatch(r"(?:bmd|dmo)(?:gcm2)?[0-9]*", key):
        return "bmd"
    if re.fullmatch(r"(?:area|cmo|bmc)(?:cm2|g)?[0-9]*", key):
        return "other"
    if "%" in text or key in ("pr", "am"):
        return "percent"
    return None


def _join(tokens: list[OCRToken]) -> OCRToken:
    return OCRToken(" ".join(t.text for t in tokens), min(t.left for t in tokens),
                    min(t.top for t in tokens), max(t.right for t in tokens),
                    max(t.bottom for t in tokens), min(t.confidence for t in tokens))


def _headers(tokens: list[OCRToken]) -> list[tuple[str, OCRToken]]:
    found = [(kind, t) for t in tokens if t.confidence >= .4
             and (kind := _header_kind(t.text))]
    # Tesseract may split 'Escore T' / 'T - score' into separate words.
    for t in tokens:
        if t.confidence < .4 or _key(t.text) not in ("t", "z", "escore", "score"):
            continue
        neighbors = sorted([s for s in tokens if s.confidence >= .4
                            and s.left >= t.left and s.right <= t.right + 8*t.height
                            and abs(s.y-t.y) < .6*max(t.height, s.height)], key=lambda s:s.left)
        for count in (2, 3):
            if len(neighbors) >= count:
                combined = _join(neighbors[:count])
                kind = _header_kind(combined.text)
                if kind in ("t", "z"):
                    found.append((kind, combined))
                    break
    return found


def extract_table(tokens: list[OCRToken]) -> TableExtraction:
    """Return canonical GE-five-column text, or warnings with no guessed scores.

    Supported headers: BMD/DMO, T-score/Escore T, Z-score/Escore Z; optional
    percentage columns. A unique table is required. Other layouts fail closed.
    """
    result = TableExtraction()
    headers = _headers(tokens)
    candidates = []
    for kt, t in headers:
        if kt != "t":
            continue
        for kz, z in headers:
            h = max(t.height, z.height)
            if kz != "z" or abs(t.y-z.y) > 1.5*h or abs(t.x-z.x) < 3*h:
                continue
            bmds = [b for k,b in headers if k == "bmd"
                    and -1.5*h <= t.y-b.y <= 5*h
                    and b.x < min(t.x,z.x)-2*h]
            if len(bmds) == 1:
                candidates.append({"bmd": bmds[0], "t":t, "z":z})
    if len(candidates) != 1:
        result.warnings.append("Tabela sem cabeçalhos DMO/T-score/Z-score inequívocos; revisão necessária")
        return result
    columns = candidates[0]
    h = median(t.height for t in columns.values())
    header_bottom = max(t.bottom for t in columns.values())
    header_y = max(columns["t"].y, columns["z"].y)
    # Percentages are optional; map each to the adjacent score, not to a
    # presumed numeric offset. Ignore unit exponents and footnote numbers.
    percents = [t for k,t in headers if k == "percent" and abs(t.y-header_y) <= 1.5*h]
    for name in ("t", "z"):
        score = columns[name]
        explicit = [p for p in percents if _key(p.text) == ("pr" if name == "t" else "am")]
        nearby = [p for p in percents if _key(p.text) not in ("pr", "am")
                  and columns["bmd"].x < p.x < score.x
                  and not any(p.x < other.x < score.x for other in columns.values())]
        if len(explicit) == 1:
            columns[name+"_percent"] = explicit[0]
        elif len(nearby) == 1:
            columns[name+"_percent"] = nearby[0]
    # Only narrow, disjoint windows around headers are accepted. A missing
    # score cannot consume a percentage or another score in the same row.
    anchors = list(columns.values()) + [t for k,t in headers if k in ("other", "percent")
                                       and abs(t.y-header_y) <= 5*h]
    radii = {name: min(3*h, .42*min(abs(t.x-other.x)
               for other in anchors if abs(t.x-other.x) > 1))
             for name,t in columns.items()}
    left_limit = columns["bmd"].x - radii["bmd"]
    stop = min((t.top for t in tokens if t.top > header_bottom
                and re.match(r"(?i)^(?:tend[eê]ncia|trend|coment[aá]rios|comments|[1-9]\s*[-–]\s*[A-Za-z])", t.text)),
               default=header_bottom + 40*h)
    data = sorted([t for t in tokens if header_bottom < t.y < stop], key=lambda t:t.y)
    lines: list[list[OCRToken]] = []
    for t in data:
        if not lines or abs(t.y-median(s.y for s in lines[-1])) > .65*h:
            lines.append([t])
        else:
            lines[-1].append(t)
    previous_y = header_bottom
    rows = []
    for line in lines:
        labels = sorted([t for t in line if t.right < left_limit and t.confidence >= .65
                         and not re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?|-", t.text)], key=lambda t:t.left)
        region = " ".join(t.text for t in labels).strip().rstrip(".,")
        if not region or not any(p.fullmatch(region) for p in (_LUMBAR_PATTERN, _FEMUR_PATTERN, _FOREARM_PATTERN)):
            if region and any(abs(t.x-columns["bmd"].x) <= radii["bmd"]
                              and re.fullmatch(r"\d+[.,]\d+", t.text) for t in line):
                result.warnings.append("Linha com região anatômica ilegível; revisão necessária")
            continue
        y = median(t.y for t in line)
        if y-previous_y > 8*h:
            # Do not pick up a later trend table or footnote containing Total.
            break
        previous_y = y
        cells = {}
        for name, anchor in columns.items():
            pieces = sorted([t for t in line if abs(t.x-anchor.x) <= radii[name]], key=lambda t:t.left)
            value = "".join(t.text.strip() for t in pieces).replace("−", "-").replace("–", "-")
            if (not pieces or any(t.confidence < .65 for t in pieces)
                    or not re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?|-", value)):
                cells[name] = "-"
                result.warnings.append(f"{region}: célula {name} ausente/ambígua; revisão necessária")
            else:
                cells[name] = value
        rows.append(" ".join([region, cells["bmd"], cells.get("t_percent", "-"),
                              cells["t"], cells.get("z_percent", "-"), cells["z"]]))
    result.text = "\n".join(rows)
    if not rows:
        result.warnings.append("Nenhuma linha alinhada aos cabeçalhos; revisão necessária")
    return result
