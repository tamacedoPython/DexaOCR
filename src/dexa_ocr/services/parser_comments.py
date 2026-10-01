"""Parser para a seção de comentários do relatório DXA."""

from __future__ import annotations

import re

from ..utils.text_utils import clean_whitespace
from ..utils.logger import get_logger

log = get_logger("parser_comments")


def parse_comments(text: str) -> str:
    """Limpa e normaliza o texto da seção de comentários.

    Remove o prefixo "COMENTÁRIOS:" e artefatos visuais comuns do OCR.
    Retorna string vazia se não houver conteúdo útil.
    """
    if not text or not text.strip():
        return ""

    # Remover prefixo
    cleaned = re.sub(
        r"^COMENT[AÁ]RIO[S\"':]*\s*",
        "",
        text.strip(),
        flags=re.IGNORECASE | re.MULTILINE,
    )

    # Remover artefatos comuns de OCR (pipes, barras, caracteres isolados)
    cleaned = re.sub(r"^[|/\\\"'\s]+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"[|/\\\"']+$", "", cleaned, flags=re.MULTILINE)

    # Limpar whitespace
    cleaned = clean_whitespace(cleaned)

    # Remover aspas envolventes
    cleaned = cleaned.strip('"\'')

    if len(cleaned) < 5:
        log.debug("Comentários muito curtos ou vazios após limpeza")
        return ""

    log.info("Comentários extraídos (%d caracteres)", len(cleaned))
    return cleaned
