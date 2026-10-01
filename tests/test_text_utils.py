"""Testes para utilidades de texto OCR."""

import pytest
from src.dexa_ocr.utils.text_utils import (
    fix_ocr_chars,
    normalize_decimal,
    normalize_int,
    clean_whitespace,
    is_plausible_bmd,
    is_plausible_score,
    is_plausible_percent,
)


class TestFixOcrChars:
    """Testes para correção de caracteres OCR em contexto numérico."""

    def test_O_to_0(self):
        assert fix_ocr_chars("O,775") == "0,775"

    def test_semicolon_to_comma(self):
        assert fix_ocr_chars("0;757") == "0,757"

    def test_mixed_corrections(self):
        result = fix_ocr_chars("O;757")
        assert result == "0,757"

    def test_preserves_text_tokens(self):
        """Tokens puramente textuais não devem ser alterados."""
        result = fix_ocr_chars("Paciente Maria")
        assert result == "Paciente Maria"

    def test_negative_number(self):
        result = fix_ocr_chars("-3,0")
        assert result == "-3,0"

    def test_pipe_to_1(self):
        result = fix_ocr_chars("|,07|")
        assert result == "1,071"


class TestNormalizeDecimal:
    """Testes para normalização de números decimais."""

    def test_comma_separator(self):
        assert normalize_decimal("0,775") == 0.775

    def test_dot_separator(self):
        assert normalize_decimal("0.775") == 0.775

    def test_negative(self):
        assert normalize_decimal("-3,0") == -3.0

    def test_trailing_dot(self):
        assert normalize_decimal("0.831.") == 0.831

    def test_dash_returns_none(self):
        assert normalize_decimal("-") is None

    def test_empty_returns_none(self):
        assert normalize_decimal("") is None

    def test_garbage_returns_none(self):
        assert normalize_decimal("abc") is None

    def test_integer(self):
        assert normalize_decimal("76") == 76.0

    def test_space_in_number(self):
        assert normalize_decimal("0, 775") == 0.775


class TestNormalizeInt:
    """Testes para normalização de inteiros."""

    def test_simple_int(self):
        assert normalize_int("76") == 76

    def test_trailing_comma(self):
        assert normalize_int("72,") == 72

    def test_trailing_dot(self):
        assert normalize_int("72.") == 72

    def test_dash_returns_none(self):
        assert normalize_int("-") is None

    def test_float_truncated(self):
        # "83.5" → 84 (arredondado)
        assert normalize_int("83.5") == 84


class TestCleanWhitespace:
    """Testes para limpeza de whitespace."""

    def test_collapses_spaces(self):
        assert clean_whitespace("a   b") == "a b"

    def test_collapses_tabs(self):
        assert clean_whitespace("a\t\tb") == "a b"

    def test_removes_extra_newlines(self):
        assert clean_whitespace("a\n\n\n\nb") == "a\n\nb"

    def test_strips(self):
        assert clean_whitespace("  hello  ") == "hello"


class TestPlausibility:
    """Testes para validadores de plausibilidade."""

    def test_bmd_valid(self):
        assert is_plausible_bmd(0.775) is True
        assert is_plausible_bmd(1.2) is True

    def test_bmd_invalid(self):
        assert is_plausible_bmd(0.0) is False
        assert is_plausible_bmd(5.0) is False
        assert is_plausible_bmd(None) is False

    def test_score_valid(self):
        assert is_plausible_score(-3.0) is True
        assert is_plausible_score(0.1) is True
        assert is_plausible_score(-6.5) is True

    def test_score_invalid(self):
        assert is_plausible_score(-8.0) is False
        assert is_plausible_score(10.0) is False
        assert is_plausible_score(None) is False

    def test_percent_valid(self):
        assert is_plausible_percent(76) is True
        assert is_plausible_percent(102) is True

    def test_percent_invalid(self):
        assert is_plausible_percent(0) is False
        assert is_plausible_percent(300) is False
        assert is_plausible_percent(None) is False
