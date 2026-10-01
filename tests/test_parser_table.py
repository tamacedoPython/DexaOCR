"""Testes para parser de tabelas densitométricas."""

import pytest
from src.dexa_ocr.services.parser_table import (
    parse_measurement_table,
    detect_site_type,
    build_site_result,
)


class TestDetectSiteType:
    """Testes para detecção de tipo de sítio anatômico."""

    def test_coluna_ap(self):
        assert detect_site_type("Coluna AP Densidade Óssea") == "lumbar_spine"

    def test_femur_direito(self):
        assert detect_site_type("Fêmur direito Densidade Óssea") == "right_femur"

    def test_femur_esquerdo(self):
        assert detect_site_type("Fêmur esquerdo Densidade Óssea") == "left_femur"

    def test_l2_l4_reference(self):
        assert detect_site_type("Referência de Densitometria: L2-L4 (BMD)") == "lumbar_spine"

    def test_unknown(self):
        assert detect_site_type("algo aleatório") == "unknown"


class TestParseMeasurementTableLumbar:
    """Testes para parsing de tabela da coluna lombar."""

    SAMPLE_TEXT = """\
L1 0,775 69 -3,0 76 -2,1
L2 0,757 63 -3,7 69 -2,9
L3 0,829 69 -3,1 75 -2,3
L4 0,916 76 -2,4 83 -1,5
L1-L2 0,765 66 -3,3 72 -2,5
L2-L3 0,790 66 -3,4 72 -2,6
L2-L4 0,831 69 -3,1 76 -2,2
"""

    def test_extracts_all_rows(self):
        rows = parse_measurement_table(self.SAMPLE_TEXT, "lumbar_spine")
        assert len(rows) == 7

    def test_l1_values(self):
        rows = parse_measurement_table(self.SAMPLE_TEXT, "lumbar_spine")
        l1 = next(r for r in rows if r.region == "L1")
        assert l1.bmd == 0.775
        assert l1.young_adult_percent == 69
        assert l1.t_score == -3.0
        assert l1.age_matched_percent == 76
        assert l1.z_score == -2.1

    def test_l4_values(self):
        rows = parse_measurement_table(self.SAMPLE_TEXT, "lumbar_spine")
        l4 = next(r for r in rows if r.region == "L4")
        assert l4.bmd == 0.916
        assert l4.t_score == -2.4

    def test_combined_region(self):
        rows = parse_measurement_table(self.SAMPLE_TEXT, "lumbar_spine")
        l2_l4 = next(r for r in rows if r.region == "L2-L4")
        assert l2_l4.bmd == 0.831

    def test_noisy_ocr_text(self):
        """OCR ruidoso: ponto-e-vírgula, pontos extras."""
        noisy = "L1 0;775 69 -3,0 76 -2,1\nL2. 0,757 63 -3,7 69 -2,9\n"
        rows = parse_measurement_table(noisy, "lumbar_spine")
        assert len(rows) >= 1
        l1 = rows[0]
        assert l1.region == "L1"


class TestParseMeasurementTableFemur:
    """Testes para parsing de tabela do fêmur."""

    SAMPLE_TEXT = """\
Colo 0,747 72 -2,1 78 -1,5
Wards 0,779 86 -1,0 102 0,1
Troc. 0,679 80 -1,5 86 -0,9
Diafise 1,071 - - - -
Total 0,875 87 -1,1 90 -0,8
"""

    def test_extracts_all_rows(self):
        rows = parse_measurement_table(self.SAMPLE_TEXT, "right_femur")
        assert len(rows) == 5

    def test_colo_values(self):
        rows = parse_measurement_table(self.SAMPLE_TEXT, "right_femur")
        colo = next(r for r in rows if r.region == "Colo")
        assert colo.bmd == 0.747
        assert colo.t_score == -2.1
        assert colo.z_score == -1.5

    def test_diafise_null_values(self):
        """Diáfise tem valores '-' que devem ser None."""
        rows = parse_measurement_table(self.SAMPLE_TEXT, "right_femur")
        diafise = next(r for r in rows if r.region == "Diáfise")
        assert diafise.bmd == 1.071
        assert diafise.t_score is None
        assert diafise.z_score is None

    def test_total_values(self):
        rows = parse_measurement_table(self.SAMPLE_TEXT, "right_femur")
        total = next(r for r in rows if r.region == "Total")
        assert total.bmd == 0.875
        assert total.t_score == -1.1

    def test_wards_positive_z_score(self):
        rows = parse_measurement_table(self.SAMPLE_TEXT, "right_femur")
        wards = next(r for r in rows if r.region == "Wards")
        assert wards.z_score == 0.1


class TestBuildSiteResult:
    """Testes para construção de SiteResult."""

    def test_lumbar_site(self):
        result = build_site_result(
            site_title_text="Coluna AP Densidade Óssea",
            table_header_text="Região (g/cm²) (%) T-score (%) Z-score",
            table_data_text="L1 0,775 69 -3,0 76 -2,1\nL2 0,757 63 -3,7 69 -2,9",
        )
        assert result is not None
        assert result.site_type == "lumbar_spine"
        assert len(result.rows) == 2

    def test_femur_site(self):
        result = build_site_result(
            site_title_text="Fêmur direito Densidade Óssea",
            table_header_text="Região BMD T-score Z-score",
            table_data_text="Colo 0,747 72 -2,1 78 -1,5\nTotal 0,875 87 -1,1 90 -0,8",
        )
        assert result is not None
        assert result.site_type == "right_femur"
        assert len(result.rows) == 2

    def test_unknown_site_returns_none(self):
        result = build_site_result(
            site_title_text="Algo desconhecido",
            table_header_text="",
            table_data_text="nenhum dado útil",
        )
        assert result is None


class TestDetectSiteTypeForearm:
    """Testes para detecção de sítio antebraço."""

    def test_antebraco(self):
        assert detect_site_type("Antebraço Densitometria Óssea") == "forearm"

    def test_forearm_english(self):
        assert detect_site_type("Forearm Bone Density") == "forearm"

    def test_antebraco_direito(self):
        assert detect_site_type("Antebraço Direito") == "right_forearm"

    def test_antebraco_esquerdo(self):
        assert detect_site_type("Antebraço Esquerdo") == "left_forearm"

    def test_radio_ulna_from_content(self):
        """Detecta antebraço pelo conteúdo quando o título não está claro."""
        content = "Rádio 1/3 0.786 85 -1.6 89 -1.1\nUlna Total 0.467 85 -1.5"
        assert detect_site_type(content) == "forearm"

    def test_ulna_keyword(self):
        assert detect_site_type("Rádio Ulna DMO") == "forearm"


class TestParseMeasurementTableForearm:
    """Testes para parsing da tabela de antebraço (GE Lunar)."""

    SAMPLE_GE = """\
Rádio 1/3   0,786   85   -1,6   89   -1,1
Rádio Médio  0,457   82   -1,8   87   -1,3
Rádio UD     0,345   78   -2,0   82   -1,5
Rádio Total  0,567   83   -1,7   88   -1,2
Ulna Total   0,467   85   -1,5   89   -1,0
Total        0,556   84   -1,7   88   -1,2
"""

    def test_extrai_todas_as_linhas(self):
        rows = parse_measurement_table(self.SAMPLE_GE, "forearm")
        assert len(rows) == 6

    def test_radio_1_3(self):
        rows = parse_measurement_table(self.SAMPLE_GE, "forearm")
        r = next((r for r in rows if r.region == "Rádio 1/3"), None)
        assert r is not None
        assert r.bmd == 0.786
        assert r.young_adult_percent == 85
        assert r.t_score == -1.6
        assert r.age_matched_percent == 89
        assert r.z_score == -1.1

    def test_radio_medio(self):
        rows = parse_measurement_table(self.SAMPLE_GE, "forearm")
        r = next((r for r in rows if r.region == "Rádio Médio"), None)
        assert r is not None
        assert r.bmd == 0.457
        assert r.t_score == -1.8

    def test_radio_ud(self):
        rows = parse_measurement_table(self.SAMPLE_GE, "forearm")
        r = next((r for r in rows if r.region == "Rádio UD"), None)
        assert r is not None
        assert r.bmd == 0.345

    def test_ulna_total(self):
        rows = parse_measurement_table(self.SAMPLE_GE, "forearm")
        r = next((r for r in rows if r.region == "Ulna Total"), None)
        assert r is not None
        assert r.bmd == 0.467

    def test_total_forearm(self):
        rows = parse_measurement_table(self.SAMPLE_GE, "forearm")
        r = next((r for r in rows if r.region == "Total"), None)
        assert r is not None
        assert r.bmd == 0.556

    def test_build_site_result_forearm(self):
        result = build_site_result(
            site_title_text="Antebraço Densidade Óssea",
            table_header_text="Região DMO(g/cm²) %JA T-score %AM Z-score",
            table_data_text=self.SAMPLE_GE,
        )
        assert result is not None
        assert result.site_type == "forearm"
        assert result.site_name == "Antebraço"
        assert len(result.rows) == 6

    def test_build_site_result_forearm_direito(self):
        result = build_site_result(
            site_title_text="Antebraço Direito Densidade Óssea",
            table_header_text="",
            table_data_text=self.SAMPLE_GE,
        )
        assert result is not None
        assert result.site_type == "right_forearm"
        assert result.site_name == "Antebraço Direito"
