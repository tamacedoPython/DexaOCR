"""Utilidades para correção e normalização de texto OCR."""

from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Correção de caracteres comuns do OCR
# ---------------------------------------------------------------------------

# Mapeamento de substituições que só devem ser aplicadas em contexto numérico.
_NUMERIC_CHAR_MAP: dict[str, str] = {
    "O": "0",
    "o": "0",
    "Q": "0",
    "l": "1",
    "I": "1",
    "|": "1",
    "!": "1",
    "S": "5",
    "s": "5",
    "B": "8",
    ";": ",",
    ":": ".",
}


def fix_ocr_chars(text: str) -> str:
    """Aplica substituições de caracteres comuns em tokens que parecem numéricos.

    Percorre cada token separado por espaço.  Se o token parecer numérico
    (contém ao menos um dígito ou parece um número com ruído), aplica o mapa.
    Tokens puramente alfabéticos são mantidos intactos.
    """
    tokens = text.split()
    fixed: list[str] = []
    for tok in tokens:
        if _looks_numeric(tok):
            fixed.append(_apply_char_map(tok))
        else:
            fixed.append(tok)
    return " ".join(fixed)


def _looks_numeric(token: str) -> bool:
    """Retorna True se o token parece ser (ou deveria ser) um número.

    Critério: contém ao menos um dígito, OU é um token curto (<=6 chars)
    composto majoritariamente de caracteres que poderiam ser dígitos.
    """
    if any(c.isdigit() for c in token):
        return True
    # Token curto onde mais da metade dos chars estão no mapa de substituição
    if len(token) <= 6:
        mapped = sum(1 for c in token if c in _NUMERIC_CHAR_MAP or c in ".,- ")
        return mapped > len(token) * 0.5
    return False


def _apply_char_map(token: str) -> str:
    """Aplica substituições caractere a caractere em um token numérico."""
    result: list[str] = []
    for ch in token:
        result.append(_NUMERIC_CHAR_MAP.get(ch, ch))
    return "".join(result)


# ---------------------------------------------------------------------------
# Normalização de números decimais
# ---------------------------------------------------------------------------

def normalize_decimal(text: str) -> Optional[float]:
    """Converte uma string numérica para float, aceitando vírgula ou ponto.

    Retorna None se não for possível converter.
    """
    if not text or text.strip() == "-":
        return None

    cleaned = text.strip()

    # -----------------------------------------------------------------------
    # Artefato específico do scanner GE Lunar DPX com PaddleOCR:
    # O valor "-0,6" (e similares) é lido como "6'0-" — sinal invertido para
    # o fim, separador decimal virou apóstrofe/backtick, dígitos trocados.
    # Padrão: \d['\`]\d+-  →  -\2.\1  (ex: "6'0-" → "-0.6", "9'0-" → "-0.9")
    # Aplicar antes de qualquer outra transformação, usando o token original.
    _GE_INVERTED = re.match(
        r"^(\d+)['\u2018\u2019\u201c\u201d`](\d+)-$",
        cleaned,
    )
    if _GE_INVERTED:
        try:
            recovered = float(f"-{_GE_INVERTED.group(2)}.{_GE_INVERTED.group(1)}")
            return recovered
        except ValueError:
            pass  # continua com o fluxo normal

    # Remover aspas tipográficas e backticks que o OCR produz ao redor de números
    # (ex.: "'6'0-'" → "60-", que depois de outros tratamentos → "-0.6" ou similar)
    cleaned = re.sub(r"[\u2018\u2019\u201c\u201d\u00b4`'\"]", "", cleaned)
    # Remover espaços internos (ex.: "0, 775" → "0,775")
    cleaned = cleaned.replace(" ", "")
    # Aceitar vírgula como separador decimal
    cleaned = cleaned.replace(",", ".")
    # Remover pontos duplos (ex.: "0..775" ou "0.831." → "0.831")
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    cleaned = cleaned.rstrip(".")
    # Tratamento de sinal no fim: OCR às vezes lê "-0,6" como "0,6-".
    # Mover o sinal para o início se o número ainda não tiver sinal.
    if cleaned.endswith("-") and not cleaned.startswith("-"):
        cleaned = "-" + cleaned[:-1]

    try:
        return float(cleaned)
    except ValueError:
        return None


def normalize_int(text: str) -> Optional[int]:
    """Converte uma string para inteiro, tratando ruído OCR.

    Retorna None se não for possível converter.
    """
    if not text or text.strip() == "-":
        return None

    cleaned = text.strip().replace(" ", "")
    # Remover separador decimal se for ",0" ou ".0" (ex.: "72," → "72")
    cleaned = cleaned.rstrip(",.")

    try:
        return round(float(cleaned))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Limpeza de whitespace
# ---------------------------------------------------------------------------

def clean_whitespace(text: str) -> str:
    """Colapsa espaços e tabs múltiplos em um único espaço, remove linhas vazias duplicadas."""
    # Colapsar espaços/tabs na mesma linha
    text = re.sub(r"[ \t]+", " ", text)
    # Remover linhas em branco duplicadas
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Validadores de plausibilidade
# ---------------------------------------------------------------------------

def is_plausible_bmd(value: Optional[float]) -> bool:
    """BMD plausível: entre 0.05 e 2.5 g/cm²."""
    if value is None:
        return False
    return 0.05 <= value <= 2.5


def is_plausible_score(value: Optional[float]) -> bool:
    """T-score ou Z-score plausível: entre -7.0 e +7.0."""
    if value is None:
        return False
    return -7.0 <= value <= 7.0


def is_plausible_percent(value: Optional[int]) -> bool:
    """Percentual (jovem adulto ou corrigido para idade): entre 30 e 250.

    O valor mínimo clinicamente realista em densitometria é ~50% (osteoporose
    severa grave, T-score ≈ -5). Valores abaixo de 30 são invariavelmente erros
    de OCR — geralmente um decimal lido errado (ex.: '9.3' no lugar de '93').
    """
    if value is None:
        return False
    return 30 <= value <= 250
