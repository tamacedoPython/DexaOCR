"""Testes para parser de dados do paciente."""

import pytest
from src.dexa_ocr.services.parser_header import parse_patient_info


SAMPLE_HEADER = """
Rua João Veras de Siqueira, S/N, Bairro Jardim Primavera, BR 116 - Salgueiro-PE
Fone (87)3871-6000
"""

SAMPLE_PATIENT_INFO = """
Paciente: MARIA DO CARMO DE SA ID Estabelecimento:
Data de Nascimento: 19/11/1959 66,4 anos Médico que JAILSON JOSE
Altura / Peso: 167,0cm 67,0 kg Medido: 16/04/2026 08:14:20 (13,60)
Sexo / Etnia: Feminino Negro Analisado: 16/04/2026 11:28:56 (13,60)
"""


class TestParsePatientInfo:
    """Testes para extração de dados do paciente."""

    def test_name(self):
        info = parse_patient_info(SAMPLE_HEADER, SAMPLE_PATIENT_INFO)
        assert info.name is not None
        assert "MARIA" in info.name
        assert "CARMO" in info.name

    def test_birth_date(self):
        info = parse_patient_info(SAMPLE_HEADER, SAMPLE_PATIENT_INFO)
        assert info.birth_date == "19/11/1959"

    def test_age(self):
        info = parse_patient_info(SAMPLE_HEADER, SAMPLE_PATIENT_INFO)
        assert info.age == "66.4"

    def test_sex(self):
        info = parse_patient_info(SAMPLE_HEADER, SAMPLE_PATIENT_INFO)
        assert info.sex == "Feminino"

    def test_ethnicity(self):
        info = parse_patient_info(SAMPLE_HEADER, SAMPLE_PATIENT_INFO)
        assert info.ethnicity == "Negro"

    def test_height(self):
        info = parse_patient_info(SAMPLE_HEADER, SAMPLE_PATIENT_INFO)
        assert info.height_cm == 167.0

    def test_weight(self):
        info = parse_patient_info(SAMPLE_HEADER, SAMPLE_PATIENT_INFO)
        assert info.weight_kg == 67.0

    def test_physician(self):
        info = parse_patient_info(SAMPLE_HEADER, SAMPLE_PATIENT_INFO)
        assert info.physician is not None
        assert "JAILSON" in info.physician

    def test_measured_at(self):
        info = parse_patient_info(SAMPLE_HEADER, SAMPLE_PATIENT_INFO)
        assert info.measured_at == "16/04/2026 08:14:20"

    def test_analyzed_at(self):
        info = parse_patient_info(SAMPLE_HEADER, SAMPLE_PATIENT_INFO)
        assert info.analyzed_at == "16/04/2026 11:28:56"

    def test_noisy_ocr_name(self):
        """OCR com ruído no nome: '»» MARIA DO CARMO DE:SA.'"""
        noisy_patient = """
        Paciente: »» MARIA DO CARMO DE:SA. ID Estabelecimento:
        Data de Nascimento: (19/11/1959 66,4 anos
        """
        info = parse_patient_info("", noisy_patient)
        assert info.name is not None
        assert "MARIA" in info.name

    def test_missing_fields_return_none(self):
        """Texto vazio não deve causar exceção."""
        info = parse_patient_info("", "")
        assert info.name is None
        assert info.birth_date is None
        assert info.height_cm is None
