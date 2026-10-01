"""Testes para modelos de dados DXA e serialização JSON."""

import pytest
from src.dexa_ocr.models.dxa_models import (
    PatientInfo,
    MeasurementRow,
    SiteResult,
    DxaReport,
    OCRMetadata,
)


class TestPatientInfo:
    def test_to_dict(self):
        info = PatientInfo(name="João", birth_date="01/01/1990", age="35.0")
        d = info.to_dict()
        assert d["name"] == "João"
        assert d["birth_date"] == "01/01/1990"
        assert d["sex"] is None  # campo não preenchido

    def test_all_none(self):
        info = PatientInfo()
        d = info.to_dict()
        assert all(v is None for v in d.values())


class TestMeasurementRow:
    def test_to_dict(self):
        row = MeasurementRow(
            region="L1", bmd=0.775, young_adult_percent=69,
            t_score=-3.0, age_matched_percent=76, z_score=-2.1,
        )
        d = row.to_dict()
        assert d["region"] == "L1"
        assert d["bmd"] == 0.775
        assert d["t_score"] == -3.0

    def test_partial_values(self):
        row = MeasurementRow(region="Diáfise", bmd=1.071)
        d = row.to_dict()
        assert d["bmd"] == 1.071
        assert d["t_score"] is None
        assert d["z_score"] is None


class TestDxaReport:
    def test_to_dict_structure(self):
        report = DxaReport(
            patient_info=PatientInfo(name="Maria", sex="Feminino"),
            sites=[
                SiteResult(
                    site_name="Coluna Lombar",
                    site_type="lumbar_spine",
                    rows=[
                        MeasurementRow(region="L1", bmd=0.775, t_score=-3.0, z_score=-2.1),
                        MeasurementRow(region="L2", bmd=0.757, t_score=-3.7, z_score=-2.9),
                    ],
                ),
                SiteResult(
                    site_name="Fêmur Direito",
                    site_type="right_femur",
                    rows=[
                        MeasurementRow(region="Colo", bmd=0.747, t_score=-2.1, z_score=-1.5),
                    ],
                ),
            ],
            comments="Imagem destinada a diagnóstico.",
            ocr_metadata=OCRMetadata(engine="tesseract", pages_processed=2),
        )

        d = report.to_dict()

        # Estrutura raiz
        assert "patient_info" in d
        assert "lumbar_spine" in d
        assert "right_femur" in d
        assert "comments" in d
        assert "ocr_metadata" in d

        # Paciente
        assert d["patient_info"]["name"] == "Maria"
        assert d["patient_info"]["sex"] == "Feminino"

        # Coluna lombar
        assert "L1" in d["lumbar_spine"]
        assert d["lumbar_spine"]["L1"]["bmd"] == 0.775
        assert d["lumbar_spine"]["L1"]["t_score"] == -3.0

        # Fêmur
        assert "Colo" in d["right_femur"]
        assert d["right_femur"]["Colo"]["bmd"] == 0.747

        # Metadata
        assert d["ocr_metadata"]["engine"] == "tesseract"
        assert d["ocr_metadata"]["pages_processed"] == 2

    def test_empty_report(self):
        report = DxaReport()
        d = report.to_dict()
        assert d["patient_info"]["name"] is None
        assert d["comments"] is None
        assert d["ocr_metadata"]["engine"] == "tesseract"

    def test_null_comments(self):
        report = DxaReport(comments="")
        d = report.to_dict()
        assert d["comments"] is None  # string vazia → null
