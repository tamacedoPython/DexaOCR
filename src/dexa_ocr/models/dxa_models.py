"""Modelos de dados para relatórios DXA (densitometria óssea)."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class PatientInfo:
    """Dados demográficos e administrativos do paciente."""

    name: Optional[str] = None
    birth_date: Optional[str] = None
    age: Optional[str] = None
    sex: Optional[str] = None
    ethnicity: Optional[str] = None
    height_cm: Optional[float] = None
    weight_kg: Optional[float] = None
    physician: Optional[str] = None
    measured_at: Optional[str] = None
    analyzed_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MeasurementRow:
    """Uma linha da tabela densitométrica (ex.: L1, Colo, Total).

    Cada campo numérico é Optional porque o OCR pode não conseguir ler
    ou porque o equipamento não reporta aquele valor (ex.: Diáfise sem T-score).
    """

    region: str = ""
    bmd: Optional[float] = None
    young_adult_percent: Optional[int] = None
    t_score: Optional[float] = None
    age_matched_percent: Optional[int] = None
    z_score: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SiteResult:
    """Resultado de um sítio anatômico (coluna lombar, fêmur dir/esq)."""

    site_name: str = ""
    site_type: str = ""  # "lumbar_spine", "right_femur", "left_femur"
    rows: list[MeasurementRow] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "site_name": self.site_name,
            "site_type": self.site_type,
            "rows": {row.region: _row_values(row) for row in self.rows},
        }


@dataclass
class OCRMetadata:
    """Metadados sobre a execução do OCR."""

    engine: str = "tesseract"
    pages_processed: int = 0
    pages_skipped: int = 0
    confidence_notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DxaReport:
    """Relatório DXA completo — modelo raiz do JSON de saída."""

    patient_info: PatientInfo = field(default_factory=PatientInfo)
    sites: list[SiteResult] = field(default_factory=list)
    comments: str = ""
    ocr_metadata: OCRMetadata = field(default_factory=OCRMetadata)

    def to_dict(self) -> dict:
        """Serializa para o formato JSON padronizado."""
        result: dict = {
            "patient_info": self.patient_info.to_dict(),
        }

        # Monta seções por site_type
        for site in self.sites:
            result[site.site_type] = site.to_dict()["rows"]

        result["comments"] = self.comments or None
        result["ocr_metadata"] = self.ocr_metadata.to_dict()
        return result


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _row_values(row: MeasurementRow) -> dict:
    """Retorna apenas os valores numéricos de uma MeasurementRow (sem 'region')."""
    return {
        "bmd": row.bmd,
        "young_adult_percent": row.young_adult_percent,
        "t_score": row.t_score,
        "age_matched_percent": row.age_matched_percent,
        "z_score": row.z_score,
    }
