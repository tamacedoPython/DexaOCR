"""Parser para tabelas densitométricas de relatórios DXA.

Extrai linhas como L1, L2, L3, L4, L1-L4, Colo, Wards, Troc., Total
com seus valores BMD, %, T-score, %, Z-score.
"""

from __future__ import annotations

import re
from typing import Optional

from ..models.dxa_models import MeasurementRow, SiteResult
from ..utils.text_utils import (
    fix_ocr_chars,
    normalize_decimal,
    normalize_int,
    is_plausible_bmd,
    is_plausible_score,
    is_plausible_percent,
    clean_whitespace,
)
from ..utils.logger import get_logger

log = get_logger("parser_table")


# ---------------------------------------------------------------------------
# Nomes conhecidos de regiões anatômicas (para matching flexível)
# ---------------------------------------------------------------------------

_LUMBAR_REGIONS = [
    "L1", "L2", "L3", "L4",
    "L1-L2", "L1-L3", "L1-L4",
    "L2-L3", "L2-L4", "L3-L4",
]

_FEMUR_REGIONS = [
    "Colo", "Wards", "Troc.", "Troc",
    "Diáfise", "Diafise", "Total",
]

# Regex flexível para nomes de região na coluna esquerda da tabela.
# Aceita variações comuns de OCR (ex.: "Li" = L1, "L2." = L2, "Ll-L2" = L1-L2)
# "Total" cobre a linha de soma L1-L4 nos relatórios Hologic.
# \s? entre L e dígito cobre "L 1" — PaddleOCR separa o caractere 'L' do
# numeral quando a fonte é estreita ou o bloco de detecção começa na borda.
_LUMBAR_PATTERN = re.compile(
    r"(?:^|\s)((?:L\s?[1-4i|l!I](?:\s*[-–]\s*(?:L|l|1)?\s?[1-4i|l!I])?)|Total)\b",
    re.IGNORECASE,
)

_FEMUR_PATTERN = re.compile(
    r"(?:^|\s)(Colo|[Ww]ards|[Tt]roc\.?|[Dd]i[aáäà]fise|[Tt]otal)\b",
    re.IGNORECASE,
)

# Regex para regiões do antebraço/punho (GE Lunar e Hologic).
# Captura o nome completo incluindo qualificador para que match.end() aponte
# diretamente para o início dos valores numéricos na linha.
# Exemplos capturados: "Rádio 1/3", "Rádio UD", "Rádio Médio", "Rádio Total",
#                      "Ulna Total", "Ulna 1/3", "Rádio+Ulna 1/3", "Total",
#                      "1/3", "UD", "33%"
# Variantes OCR incluídas:
#   - "Rädio" (GE Lunar — OCR lê á como ä alemão)
#   - "Cübito"/"Cubito" (GE Lunar — palavra portuguesa para úlna/cubóide)
_QUALIFIER = r"(?:1\s*/\s*3|33\s*%|UD|[Mm][eé]di[ao]|[Mm]id|[Mm]eio|[Tt]otal|[Uu]ltra[-\s]*[Dd]ist\.?|[Pp]roximal)"
_FOREARM_PATTERN = re.compile(
    r"(?:^|\s)"
    r"("
    # Rádio/Rädio[+Ulna/+Cúbito] com qualificador opcional
    # Inclui variante ä (OCR GE: Rädio) e ö (Rädio → Rädio)
    r"(?:[Rr][aáä]di[oóö]|[Rr]adio)(?:\+(?:[Uu]lna|[Cc][uüú]bito))?(?:\s+" + _QUALIFIER + r")?"
    # Ulna com qualificador opcional
    r"|[Uu]lna(?:\s+" + _QUALIFIER + r")?"
    # Cúbito/Cübito/Cubito (sinônimo GE de Ulna) com qualificador opcional
    r"|[Cc][uüú]bito(?:\s+" + _QUALIFIER + r")?"
    # Qualificadores isolados (quando a linha começa diretamente pelo qualificador)
    r"|1\s*/\s*3"
    r"|33\s*%"
    r"|UD\b"
    r"|[Mm]id\b"
    r"|[Tt]otal"
    r")",
    re.IGNORECASE,
)

# Apaga a variável auxiliar do namespace do módulo
del _QUALIFIER

# Regex para detectar o início da seção de Tendência (após os dados principais)
_TREND_SECTION = re.compile(
    r"\bTend[eé]ncia\b",
    re.IGNORECASE,
)

# Notas de rodapé numeradas do GE Lunar: "1 -Estatisticamente...", "2 -EUA..."
# Aparecem depois dos dados e podem conter nomes de regiões ("Fémur direito Total",
# "Coluna AP L2-L4") que disparam falsos matches no parser.
_FOOTNOTE_START = re.compile(
    r"^\s*\d+\s*[-–]\s*[A-Za-zÀ-ÖØ-öø-ÿ]",
    re.MULTILINE,
)

# Regex para extrair um número (possivelmente com ruído OCR) de um token.
# Aceita: "0,775", "0.775", "-3,0", "-3.0", "0;757", "76", "83"
_NUMBER_TOKEN = re.compile(r"^[-+]?\d+(?:[.,;:]\d+)?$")


# ============================================================================
# Detecção de tipo de site
# ============================================================================

def detect_site_type(text: str) -> str:
    """Detecta o tipo de sítio anatômico a partir do texto do relatório.

    Procura por palavras-chave como "Coluna AP", "Fêmur direito", etc.
    no texto da ROI site_title ou no texto geral da página.

    Returns
    -------
    str
        "lumbar_spine", "right_femur", "left_femur", ou "unknown"
    """
    text_lower = text.lower()

    if any(kw in text_lower for kw in ("coluna", "lumbar", "l2-l4", "l1-l4", "l2 -")):
        return "lumbar_spine"

    # Detectar coluna lombar por vertébras individuais (L1, L2, L3, L4) na tabela
    if re.search(r"\bL[1-4]\b", text, re.IGNORECASE):
        return "lumbar_spine"

    if any(kw in text_lower for kw in ("f\u00eamur", "femur", "fémur")):
        if "esquerdo" in text_lower or "esq" in text_lower:
            return "left_femur"
        return "right_femur"

    if any(kw in text_lower for kw in ("hip", "proximal")):
        return "right_femur"

    # Detect femur from table content (Colo, Wards, Troc., Total)
    if any(kw in text_lower for kw in ("colo", "wards", "troc", "diafise", "diáfise", "diäfise")):
        return "right_femur"

    # Antebraço / punho — detecta por palavras-chave no título ou conteúdo
    # "cubito"/"cübito" = palavra portuguesa para úlna (GE Lunar)
    if any(kw in text_lower for kw in ("antebra", "forearm", "punho", "wrist", "cubito", "c\u00fcbito", "c\u00fabito")):
        if "esquerdo" in text_lower or "esq" in text_lower or "left" in text_lower:
            return "left_forearm"
        if "direito" in text_lower or "dir" in text_lower or "right" in text_lower:
            return "right_forearm"
        return "forearm"

    # Detect forearm from table content (Rádio/Rädio, Ulna)
    # Inclui variante ä (OCR GE: Rädio) e ö
    if re.search(r"r[a\u00e1\u00e4]di[o\u00f3\u00f6]|\bulna\b", text_lower):
        return "forearm"

    return "unknown"


# ============================================================================
# Parsing da tabela
# ============================================================================

def _strip_trend_section(text: str) -> str:
    """Remove a seção de tendência e as notas de rodapé numeradas do texto.

    Os relatórios GE e Hologic incluem no mesmo bloco OCR os dados da tabela
    principal seguidos de:
    - Uma seção 'Tendência: L2-L4 ...' com dados históricos.
    - Notas de rodapé numeradas ("1 -Estatisticamente...") que podem conter
      nomes de regiões como "Fémur direito Total" ou "Coluna AP L2-L4)" e
      causariam falsos matches no parser.
    """
    cut_pos: int = len(text)

    # Cortar na seção de tendência
    trend_match = _TREND_SECTION.search(text)
    if trend_match:
        cut_pos = min(cut_pos, trend_match.start())

    # Cortar na primeira nota de rodapé numerada
    footnote_match = _FOOTNOTE_START.search(text)
    if footnote_match:
        cut_pos = min(cut_pos, footnote_match.start())

    if cut_pos < len(text):
        stripped = text[:cut_pos].rstrip()
        log.debug("Trailing content removido (%d chars)", len(text) - len(stripped))
        return stripped
    return text


def parse_measurement_table(
    table_text: str,
    site_type: str,
    table_format: str = "ge",
) -> list[MeasurementRow]:
    """Extrai linhas de medição do texto OCR da tabela.

    Parameters
    ----------
    table_text : str
        Texto OCR da ROI da tabela (já recortada e pré-processada).
    site_type : str
        Tipo de sítio ("lumbar_spine", "right_femur", etc.).
    table_format : str
        Formato da tabela: 'ge' (cinco células), 'ge_compact' (DMO/T/Z)
        ou 'hologic' (sete células). Linhas incompletas não fornecem scores.

    Returns
    -------
    list[MeasurementRow]
        Lista de linhas extraídas com valores validados.
    """
    # Remover seção de tendência antes de parsear (evita sobrescrever dados bons)
    table_text = _strip_trend_section(table_text)

    lines = table_text.strip().splitlines()
    rows: list[MeasurementRow] = []
    # Rastrear regiões já inseridas — manter apenas a PRIMEIRA com dados
    seen_regions: set[str] = set()

    if "lumbar" in site_type:
        pattern = _LUMBAR_PATTERN
    elif "forearm" in site_type:
        pattern = _FOREARM_PATTERN
    else:
        pattern = _FEMUR_PATTERN

    for line_raw in lines:
        line = clean_whitespace(line_raw)
        # Descartar linhas que são apenas um número solto (ex.: marcador de rodapé "1" entre rows)
        line_stripped = line.strip()
        if re.match(r"^[\d]{1,2}$", line_stripped):
            log.debug("Linha ignorada (número isolado rodapé): '%s'", line_stripped)
            continue
        # Strip leading OCR noise (single chars, punctuation before region name)
        line = re.sub(r"^[^A-Za-z0-9]+", "", line)
        if not line or len(line) < 5:
            continue

        row = _parse_table_line(line, pattern, site_type, table_format)
        if row is not None:
            # Ignorar linhas onde todos os valores são None (falso-positivo de padrão)
            has_data = any([
                row.bmd is not None,
                row.t_score is not None,
                row.z_score is not None,
                row.young_adult_percent is not None,
                row.age_matched_percent is not None,
            ])
            if not has_data:
                log.debug("Linha '%s' ignorada: todos os valores None", row.region)
                continue
            # Deduplicar por região — manter apenas a primeira ocorrência com dados
            if row.region in seen_regions:
                log.debug("Linha '%s' ignorada: região duplicada", row.region)
                continue
            seen_regions.add(row.region)
            rows.append(row)
            log.debug("Linha extraida: %s -> %s", row.region, row.to_dict())

    if not rows:
        log.warning("Nenhuma linha de medição encontrada no texto da tabela")
    else:
        log.info("Tabela %s: %d linhas extraídas", site_type, len(rows))

    return rows


def _parse_table_line(
    line: str,
    region_pattern: re.Pattern,
    site_type: str,
    table_format: str = "ge",
) -> Optional[MeasurementRow]:
    """Tenta parsear uma única linha da tabela.

    Formato GE esperado:
        Região  BMD  %JovemAdulto  T-score  %CorrIdade  Z-score
    Formato Hologic esperado:
        Região  Área[cm²]  CMO[(g)]  DMO[g/cm²]  EscoreT  PR(%)  EscoreZ  AM(%)
    """
    match = region_pattern.search(line)
    if not match:
        return None

    region_raw = match.group(1)
    region = _normalize_region_name(region_raw, site_type)

    # Resto da linha após o nome da região
    rest = line[match.end():].strip()

    # Pré-processamento: remover ':' como prefixo de ruído OCR em cada token
    # antes de fix_ocr_chars para evitar que ':85' → '.85' (0.85) ao invés de '85'.
    # Exemplo: ':85' → '85', ':4:60' → '4:60', ':0.789' → '0.789'
    # Atenção: tokens como '-2:9' não começam com ':', portanto não são afetados.
    _pre = rest.split()
    _pre = [t[1:] if t.startswith(":") and len(t) > 1 else t for t in _pre]
    rest = " ".join(_pre)

    # Aplicar correção de caracteres OCR ao texto numérico
    rest_fixed = fix_ocr_chars(rest)

    # Tokenizar — separar por espaços
    tokens = rest_fixed.split()

    if len(tokens) < 1:
        log.debug("Linha '%s': sem tokens numéricos após região", region)
        return MeasurementRow(region=region)

    row = MeasurementRow(region=region)

    if table_format not in ("ge", "ge_compact", "hologic"):
        log.warning("Formato de tabela desconhecido: %s", table_format)
        return None

    expected = {"ge": 5, "ge_compact": 3, "hologic": 7}[table_format]
    # Read one extra value to detect overflow; never truncate and silently
    # assign shifted scores when OCR loses or adds a cell.
    values = _extract_numeric_values(tokens, max_values=expected + 1)
    if len(values) != expected:
        log.warning("Linha %s: %d células, esperado %d; scores omitidos para revisão",
                    region, len(values), expected)
        if table_format.startswith("ge") and values:
            row.bmd = _validated_bmd(values[0])
        return row

    if table_format == "ge_compact":
        row.bmd = _validated_bmd(values[0])
        row.t_score = _validated_score(values[1])
        row.z_score = _validated_score(values[2])
    elif table_format == "hologic":
        # Colunas Hologic: Área(0)  CMO(1)  DMO(2)  EscoreT(3)  PR%(4)  EscoreZ(5)  AM%(6)
        if len(values) >= 3:
            row.bmd = _validated_bmd(values[2])                        # DMO [g/cm²]
        if len(values) >= 4:
            row.t_score = _validated_score(values[3])                  # Escore T
        if len(values) >= 5:
            row.young_adult_percent = _validated_percent(values[4])    # PR (pico padrão)
        if len(values) >= 6:
            row.z_score = _validated_score(values[5])                  # Escore Z
        if len(values) >= 7:
            row.age_matched_percent = _validated_percent(values[6])    # AM (pareado por idade)
    else:
        # Colunas GE: BMD(0)  %JA(1)  T-score(2)  %AM(3)  Z-score(4)
        if len(values) >= 1:
            row.bmd = _validated_bmd(values[0])
        if len(values) >= 2:
            row.young_adult_percent = _validated_percent(values[1])
        if len(values) >= 3:
            row.t_score = _validated_score(values[2])
        if len(values) >= 4:
            row.age_matched_percent = _validated_percent(values[3])
        if len(values) >= 5:
            row.z_score = _validated_score(values[4])

    return row


def _extract_numeric_values(tokens: list[str], max_values: int = 5) -> list[Optional[float]]:
    """Extrai até max_values valores numéricos de uma lista de tokens.

    Tokens não-numéricos (ex.: "-") são preservados como None.
    """
    values: list[Optional[float]] = []
    for tok in tokens:
        if len(values) >= max_values:
            break

        tok = tok.strip("()[]|{}")

        # Artefato Hologic: PaddleOCR lê o separador de layout como ":" ou ".".
        # fix_ocr_chars converte ":" → ".", então ":0.812" vira ".0.812" (ponto
        # duplo). Strip do "." inicial quando há mais de um ponto no token.
        # Exemplos: ".0.812" → "0.812",  ".0.892" → "0.892"
        if tok.startswith(":") and len(tok) > 1:
            tok = tok[1:]
        if tok.startswith(".") and tok.count(".") > 1:
            tok = tok[1:]

        if tok in ("-", "—", "–", "_", ""):
            values.append(None)
            continue

        val = normalize_decimal(tok)
        if val is not None:
            values.append(val)
        else:
            # Token não convertível para float:
            # - token curto (<=3 chars) após ao menos 1 valor → ruído OCR no meio
            #   da linha (ex.: 'O', '|', 'l'); inserir None para NÃO deslocar as
            #   colunas subsequentes (ex.: T-score passaria a ser lido como %JA).
            # - token longo (palavra) → fim dos dados da linha; parar coleta.
            # - token curto ANTES do 1º valor → prefixo espúrio; ignorar silenciosamente.
            if len(values) > 0 and len(tok) <= 3:
                log.debug("Token curto não-numérico → None (guard contra column-shift): '%s'", tok)
                values.append(None)
            elif len(tok) > 3:
                log.debug("Token não-numérico longo '%s' — encerrando coleta de valores", tok)
                break
            else:
                log.debug("Token ignorado (não numérico, antes do 1º valor): '%s'", tok)

    return values


def _normalize_region_name(raw: str, site_type: str) -> str:
    """Normaliza o nome da região corrigindo erros comuns de OCR.

    Exemplos:
        "Li" → "L1"
        "L2." → "L2"
        "Ll-L2" → "L1-L2"
        "Troc." → "Troc."
        "Total" (lombar Hologic) → "Total"
    """
    name = raw.strip().rstrip(".")

    if "lumbar" in site_type and name.upper() == "TOTAL":
        return "Total"

    if "lumbar" in site_type:
        # Normalizar vértebras lombares
        name = name.upper()
        # Remover espaço entre L e dígito (ex.: "L 1" → "L1", "L 1-L 4" → "L1-L4")
        name = re.sub(r"L\s+(\d)", r"L\1", name)
        name = name.replace("LI", "L1").replace("L|", "L1").replace("L!", "L1")
        # Handle "L2-14" → "L2-L4" (OCR drops the 'L', reads as "14" for "L4")
        name = re.sub(r"L(\d)\s*[-–]\s*1(\d)", r"L\1-L\2", name)
        name = re.sub(r"L(\d)\s*[-–]\s*(\d)", r"L\1-L\2", name)
        # Garantir formato L1-L4 com hífen
        name = re.sub(r"L(\d)\s*[-–]\s*L(\d)", r"L\1-L\2", name)
        # Garantir que tenha "L" antes do número
        name = re.sub(r"^(\d)(-L\d)?$", r"L\1\2", name)
    elif "forearm" in site_type:
        # Regiões do antebraço — normalizar para nomes canônicos
        name_lower = name.lower().strip()
        # Identificar osso
        # Inclui variante ä (OCR GE: Rädio) e cübito/cubito (Portuguese for ulna)
        has_radio = bool(re.search(r"r[a\u00e1\u00e4]di[o\u00f3\u00f6]|radio", name_lower))
        has_ulna = "ulna" in name_lower or bool(re.search(r"c[u\u00fc\u00fa]bito", name_lower))
        has_both = has_radio and has_ulna  # ex.: "Rádio+Ulna"
        # Identificar qualificador
        if re.search(r"1\s*/\s*3|33\s*%", name_lower):
            qual = "1/3"
        elif re.search(r"\bud\b|ultra", name_lower):
            qual = "UD"
        elif re.search(r"m[eé]d|mid|meio", name_lower):
            qual = "Médio"
        elif "total" in name_lower:
            qual = "Total"
        elif "proximal" in name_lower:
            qual = "Proximal"
        else:
            qual = ""
        # Montar nome canônico
        if has_both:
            bone = "Rádio+Ulna"
        elif has_radio:
            bone = "Rádio"
        elif has_ulna:
            bone = "Ulna"
        else:
            bone = ""
        if bone and qual:
            name = f"{bone} {qual}"
        elif bone:
            name = bone
        elif qual:
            name = qual
        # else: mantém original
    else:
        # Regiões do fêmur — normalizar capitalização
        name_lower = name.lower()
        region_map = {
            "colo": "Colo",
            "wards": "Wards",
            "troc": "Troc.",
            "troc.": "Troc.",
            "diafise": "Diáfise",
            "diáfise": "Diáfise",
            "diäfise": "Diáfise",
            "total": "Total",
        }
        name = region_map.get(name_lower, name)

    return name


# ---------------------------------------------------------------------------
# Validação
# ---------------------------------------------------------------------------

def _validated_bmd(value: Optional[float]) -> Optional[float]:
    """Valida e retorna BMD, ou None se implausível."""
    if value is None:
        return None
    if is_plausible_bmd(value):
        return round(value, 3)
    # Recuperação: artefato Hologic — dígito '1' espúrio no início do número.
    # Ex.: OCR lê "0.795" como "10.795". Tentar remover o primeiro caractere.
    s = str(value)
    if 10.0 <= value < 20.0 and s[0] == "1" and len(s) > 1:
        try:
            recovered = float(s[1:])
            if is_plausible_bmd(recovered):
                log.debug("BMD: '1' inicial removida: %.4f → %.4f", value, recovered)
                return round(recovered, 3)
        except ValueError:
            pass
    # Recuperação: ponto decimal perdido pelo OCR.
    # Ex.: OCR lê "1.61" como "161" (100 ≤ value < 250 → dividir por 100).
    if 100.0 <= value < 250.0:
        recovered = round(value / 100.0, 3)
        if is_plausible_bmd(recovered):
            log.debug("BMD: ponto decimal recuperado: %.4f → %.4f", value, recovered)
            return recovered
    log.warning("BMD implausível: %.4f (esperado 0.05–2.5)", value)
    return None


def _validated_score(value: Optional[float]) -> Optional[float]:
    """Valida e retorna T-score ou Z-score, ou None se implausível."""
    if value is None:
        return None
    if is_plausible_score(value):
        return round(value, 1)
    log.warning("Score implausível: %.1f (esperado -7 a +7)", value)
    return None


def _validated_percent(value: Optional[float]) -> Optional[int]:
    """Valida e retorna percentual como inteiro, ou None se implausível."""
    if value is None:
        return None
    int_val = int(round(value))
    if is_plausible_percent(int_val):
        return int_val
    log.warning("Percentual implausível: %d (esperado 1–250)", int_val)
    return None


# ============================================================================
# Detecção de formato de tabela
# ============================================================================

def _detect_table_format(text: str) -> str:
    """Detecta formato da tabela com base no cabeçalho OCR.

    Returns 'hologic' se identificar cabeçalhos Hologic (CMO, Escore T, DMO),
    ou se as linhas de dados tiverem tipicamente 7 colunas numéricas.
    Caso contrário retorna 'ge'.
    """
    upper = text.upper()
    if any(kw in upper for kw in ("CMO", "ESCORE T", "ESCORE Z", "DMO [G")):
        return "hologic"
    # Heurística de contagem: Hologic tem 7 colunas numéricas vs 5 do GE.
    # Requer vantagem clara (2:1) para evitar falsos-positivos em linhas GE que
    # acidentalmente contenham 7+ números (ex.: linha de Tendência, rodapé).
    data_lines_ge = 0
    data_lines_hologic = 0
    for line in text.splitlines():
        nums = re.findall(r"[-+]?\d+(?:[.,]\d+)?", line)
        if len(nums) >= 7:
            data_lines_hologic += 1
        elif len(nums) >= 5:
            data_lines_ge += 1
    # Exigir maioria clara (>=2 linhas Hologic E pelo menos o dobro das GE)
    if data_lines_hologic >= 2 and data_lines_hologic >= 2 * data_lines_ge:
        return "hologic"
    return "ge"


# ============================================================================
# Construção do SiteResult
# ============================================================================

def build_site_result(
    site_title_text: str,
    table_header_text: str,
    table_data_text: str,
    table_format_hint: str = "auto",
) -> Optional[SiteResult]:
    """Constrói um SiteResult a partir dos textos OCR das ROIs relevantes.

    Parameters
    ----------
    site_title_text : str
        Texto OCR da ROI "site_title" (ex.: "Coluna AP Densidade Óssea ...").
    table_header_text : str
        Texto OCR do cabeçalho da tabela (ex.: "Região (g/cm²) ...").
    table_data_text : str
        Texto OCR da tabela de dados numéricos.
    table_format_hint : str
        Hint do fabricante detectado visualmente: 'ge', 'hologic', ou 'auto'
        para auto-deteção pelo conteúdo do OCR.

    Returns
    -------
    SiteResult | None
        Resultado estruturado, ou None se nenhum dado útil foi encontrado.
    """
    # Detectar tipo de sítio
    combined = f"{site_title_text}\n{table_header_text}"
    site_type = detect_site_type(combined)

    if site_type == "unknown":
        # Tentar detectar pelo conteúdo da própria tabela
        site_type = detect_site_type(table_data_text)

    if site_type == "unknown":
        log.warning("Não foi possível determinar o tipo de sítio anatômico")
        return None

    # Determinar nome legível
    site_name_map = {
        "lumbar_spine": "Coluna Lombar",
        "right_femur": "Fêmur Direito",
        "left_femur": "Fêmur Esquerdo",
        "right_forearm": "Antebraço Direito",
        "left_forearm": "Antebraço Esquerdo",
        "forearm": "Antebraço",
    }

    # Detectar formato da tabela (GE vs Hologic)
    if table_format_hint == "auto":
        table_format = _detect_table_format(f"{table_header_text}\n{table_data_text}")
    else:
        table_format = table_format_hint
    # Text-only callers can explicitly describe the compact GE schema.
    # Production uses spatially verified, canonical five-column rows.
    if table_format == "ge":
        header = table_header_text.lower()
        if (re.search(r"\b(?:bmd|dmo)\b", header)
                and re.search(r"\bt[\s-]*score\b", header)
                and re.search(r"\bz[\s-]*score\b", header)
                and "%" not in header):
            pattern = _LUMBAR_PATTERN if "lumbar" in site_type else (
                _FOREARM_PATTERN if "forearm" in site_type else _FEMUR_PATTERN)
            counts = []
            for line in _strip_trend_section(table_data_text).splitlines():
                match = pattern.search(line)
                if match:
                    counts.append(len(_extract_numeric_values(
                        fix_ocr_chars(line[match.end():]).split(), max_values=8)))
            if counts and all(n == 3 for n in counts):
                table_format = "ge_compact"
    log.debug("Formato de tabela: %s (hint=%s)", table_format, table_format_hint)

    rows = parse_measurement_table(table_data_text, site_type, table_format=table_format)
    if not rows:
        return None

    return SiteResult(
        site_name=site_name_map.get(site_type, site_type),
        site_type=site_type,
        rows=rows,
    )
