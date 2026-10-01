"""Parser para dados do paciente extraídos do cabeçalho do relatório DXA."""

from __future__ import annotations

import re
from typing import Optional

from ..models.dxa_models import PatientInfo
from ..utils.text_utils import normalize_decimal, clean_whitespace, fix_ocr_chars
from ..utils.logger import get_logger

log = get_logger("parser_header")


def parse_patient_info(header_text: str, patient_text: str) -> PatientInfo:
    """Extrai dados do paciente a partir do texto OCR do cabeçalho + info.

    Combina o texto de ambas as ROIs (header + patient_info) porque
    em alguns layouts os dados se espalham entre as duas faixas.

    Returns
    -------
    PatientInfo
        Estrutura com campos preenchidos (ou None para campos não encontrados).
    """
    # Combinar textos
    combined = clean_whitespace(f"{header_text}\n{patient_text}")
    log.debug("Texto combinado para parsing de paciente:\n%s", combined)

    info = PatientInfo()

    info.name = _extract_name(combined)
    info.birth_date = _extract_birth_date(combined)
    info.age = _extract_age(combined)
    info.sex = _extract_sex(combined)
    info.ethnicity = _extract_ethnicity(combined)
    info.height_cm = _extract_height(combined)
    info.weight_kg = _extract_weight(combined)
    info.physician = _extract_physician(combined)
    info.measured_at = _extract_datetime(combined, "Medido")
    info.analyzed_at = _extract_datetime(combined, "Analisado")

    _log_extraction_summary(info)
    return info


# ---------------------------------------------------------------------------
# Funções de extração individual
# ---------------------------------------------------------------------------

def _extract_name(text: str) -> Optional[str]:
    """Extrai nome do paciente: 'Paciente: NOME' (GE) ou 'Nome: NOME' (Hologic)."""
    # GE Lunar DPX: "Paciente: NOME ..."
    match = re.search(
        r"(?<!\w)Paciente\s*:\s*[.,:»\-\s]*(.*?)(?:\s+ID|\s+Estabelecimento|$)",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    # Validar se a captura GE é realmente um nome (>3 chars, não só números, não só espaços)
    if match:
        candidate = match.group(1).strip()
        if len(candidate) <= 3 or re.match(r"^\d+$", candidate):
            match = None  # Falso-positivo (ex: "ID do paciente: 123456")
    if not match:
        # Hologic Horizon: "Nome: NOME" (nome na mesma linha)
        match = re.search(
            r"(?:^|\n)Nome\s*:\s*([^\n]{3,})",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
    if not match:
        # Hologic Horizon: "Nome:\n NOME" (nome na linha seguinte)
        # Usa [^\S\n]* para evitar que \s* consuma o \n antes de \n+
        match = re.search(
            r"Nome[^\S\n]*:[^\S\n]*\n+[^\S\n]*([A-Z\u00c1\u00c9\u00cd\u00d3\u00da\u00c0\u00c2\u00ca\u00d4\u00c3\u00d5\u00c7][^\n]{2,})",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
    if match:
        name = match.group(1).strip().rstrip(".")
        # Remover caracteres espúrios do início
        name = re.sub(r"^[.,:»\-\s]+", "", name)
        # Rejeitar se parece um ID (só dígitos)
        if len(name) > 3 and not re.match(r"^\d+$", name):
            return name
    return None


def _extract_birth_date(text: str) -> Optional[str]:
    """Extrai data de nascimento: GE 'DD/MM/YYYY' ou Hologic 'DN: MM/DD/YYYY'."""
    # GE Lunar DPX: "Data de Nascimento: DD/MM/YYYY"
    match = re.search(
        r"(?:Data\s+de\s+Nascimento|Nasc\.?)\s*:\s*\(?(\d{2}/\d{2}/\d{4})\)?",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)
    # Hologic Horizon: "DN: MM/DD/YYYY" (formato americano) → converter p/ DD/MM/YYYY
    match = re.search(
        r"(?:^|\s)DN\s*:\s*(\d{2})/(\d{2})/(\d{4})",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if match:
        mm, dd, yyyy = match.group(1), match.group(2), match.group(3)
        return f"{dd}/{mm}/{yyyy}"
    # Hologic com OCR sem barras: "DN: 07291958" (MMDDYYYY = 8 dígitos)
    match = re.search(
        r"(?:^|\s)DN\s*:\s*(\d{2})(\d{2})(\d{4})(?:\D|$)",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if match:
        mm, dd, yyyy = match.group(1), match.group(2), match.group(3)
        return f"{dd}/{mm}/{yyyy}"
    return None


def _extract_age(text: str) -> Optional[str]:
    """Extrai idade: 'NN,N anos' (GE) ou 'Idade: NN' (Hologic)."""
    # GE Lunar DPX: "NN,N anos"
    match = re.search(
        r"(\d{1,3}[.,]\d)\s*anos",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).replace(",", ".")
    # Hologic Horizon: "Idade: NN" ou "idade: .67" (ponto espúiro do OCR)
    match = re.search(
        r"(?:^|\n)[Ii]dade\s*:\s*[.\s]*(\d{1,3})\b",
        text,
        re.MULTILINE,
    )
    if match:
        return match.group(1)
    return None


def _extract_sex(text: str) -> Optional[str]:
    """Extrai sexo: 'Sexo / Etnia: ...' (GE) ou 'Sexo: ...' (Hologic)."""
    # GE Lunar DPX: "Sexo / Etnia: Feminino ..."
    match = re.search(
        r"Sexo\s*/\s*Etnia\s*:\s*(\w+)",
        text,
        re.IGNORECASE,
    )
    if not match:
        # Hologic Horizon: "Sexo: Female" ou "Sexo: Feminino"
        match = re.search(
            r"(?:^|\n)Sexo\s*:\s*(\w+)",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
    if match:
        sex = match.group(1).strip()
        sex_lower = sex.lower()
        if sex_lower.startswith("fem") or sex_lower == "female":
            return "Feminino"
        if sex_lower.startswith("masc") or sex_lower == "male":
            return "Masculino"
        return sex
    return None


def _extract_ethnicity(text: str) -> Optional[str]:
    """Extrai etnia: segundo token após 'Sexo/Etnia:' (GE) ou 'Etnia: ...' (Hologic)."""
    # GE Lunar DPX: "Sexo / Etnia: Feminino ETNIA"
    match = re.search(
        r"Sexo\s*/\s*Etnia\s*:\s*\w+\s+(\w+)",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    # Hologic Horizon: "Etnia: White" ou "Etnia: Branca"
    match = re.search(
        r"(?:^|\n)Etnia\s*:\s*(\w+)",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    return match.group(1).strip() if match else None


def _extract_height(text: str) -> Optional[float]:
    """Extrai altura em cm: GE 'Altura / Peso: NNN,Ncm' ou Hologic 'Altura: NNN.N cm'."""
    # GE Lunar DPX: "Altura / Peso: NNN,Ncm"
    match = re.search(
        r"Altura\s*/\s*Peso\s*:\s*(\d{2,3}[.,]\d)\s*cm",
        text,
        re.IGNORECASE,
    )
    if match:
        return normalize_decimal(match.group(1))
    # Hologic Horizon: "Altura: NNN.N cm" ou "Altura: NNN.N.cm" (OCR com ponto extra)
    match = re.search(
        r"(?:^|\n)Altura\s*:\s*(\d{2,3}(?:[.,]\d)?)\s*[.,]?\s*cm",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if match:
        return normalize_decimal(match.group(1))
    return None


def _extract_weight(text: str) -> Optional[float]:
    """Extrai peso em kg: 'NNN,N kg'."""
    match = re.search(
        r"(\d{2,3}[.,]\d)\s*kg",
        text,
        re.IGNORECASE,
    )
    if match:
        return normalize_decimal(match.group(1))
    # Hologic: "Peso: NN.N kg" ou "Peso: NNN kg" (OCR pode omitir decimal)
    match = re.search(
        r"(?:^|\n)Peso\s*:\s*([\d.,]{2,6})\s*kg",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if match:
        return normalize_decimal(match.group(1))
    return None


def _extract_physician(text: str) -> Optional[str]:
    """Extrai nome do médico: 'Médico que NOME' (GE) ou 'Médico requisitante: NOME' (Hologic)."""
    match = re.search(
        r"[Mm][eé]dico[:\s]+(?:que\s+|requisitante[:\s]*)?([A-ZÁÉÍÓÚÀÂÊÔÃÕÇ][A-ZÁÉÍÓÚÀÂÊÔÃÕÇ .]+)",
        text,
    )
    if match:
        physician = match.group(1).strip().rstrip(".")
        if len(physician) > 2:
            return physician
    return None


def _extract_datetime(text: str, label: str) -> Optional[str]:
    """Extrai data/hora: 'Medido: DD/MM/YYYY HH:MM:SS' (ignora o que vem depois)."""
    match = re.search(
        rf"{label}\s*:\s*(\d{{2}}/\d{{2}}/\d{{4}}\s+\d{{2}}:\d{{2}}:\d{{2}})",
        text,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log_extraction_summary(info: PatientInfo) -> None:
    """Registra quais campos foram extraídos com sucesso."""
    fields = {
        "nome": info.name,
        "data_nasc": info.birth_date,
        "idade": info.age,
        "sexo": info.sex,
        "etnia": info.ethnicity,
        "altura": info.height_cm,
        "peso": info.weight_kg,
        "medico": info.physician,
        "medido": info.measured_at,
        "analisado": info.analyzed_at,
    }
    found = [k for k, v in fields.items() if v is not None]
    missing = [k for k, v in fields.items() if v is None]

    log.info("Paciente — encontrados: %s", ", ".join(found) if found else "nenhum")
    if missing:
        log.warning("Paciente — não encontrados: %s", ", ".join(missing))
