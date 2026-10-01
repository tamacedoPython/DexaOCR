"""Detecção e segmentação de ROIs (regiões de interesse) em relatórios DXA.

Estratégia principal: coordenadas percentuais fixas calibradas para
relatórios GE Lunar DPX. O layout desses relatórios é altamente
padronizado, o que torna essa abordagem robusta.

As coordenadas podem ser ajustadas via configuração se o equipamento mudar.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

import cv2
import numpy as np

from ..utils.logger import get_logger
from ..utils.image_utils import crop_roi

log = get_logger("roi_detector")


# ============================================================================
# Tipos
# ============================================================================

class PageType(enum.Enum):
    """Classificação de uma página DICOM."""
    REPORT_PAGE = "report_page"   # Página com layout de relatório (texto + tabela)
    SCAN_ONLY = "scan_only"       # Imagem anatômica pura (sem dados textuais úteis)
    UNKNOWN = "unknown"


class ROIType(enum.Enum):
    """Tipo de região de interesse."""
    HEADER = "text"
    PATIENT_INFO = "text"
    TABLE_HEADER = "text"
    TABLE_DATA = "table"
    COMMENTS = "comments"
    FOOTNOTES = "footnotes"
    ANATOMICAL = "skip"
    GRAPH = "skip"
    FOOTER = "skip"


@dataclass
class ROI:
    """Definição de uma região de interesse com coordenadas percentuais.

    Coordenadas são frações de 0.0 a 1.0 relativas ao tamanho da imagem.
    """
    name: str
    x_pct: float
    y_pct: float
    w_pct: float
    h_pct: float
    roi_type: ROIType

    @property
    def should_ocr(self) -> bool:
        """Retorna True se esta ROI deve passar por OCR."""
        return self.roi_type != ROIType.ANATOMICAL and \
               self.roi_type != ROIType.GRAPH and \
               self.roi_type != ROIType.FOOTER

    def as_tuple(self) -> tuple[str, float, float, float, float, str]:
        """Para uso com draw_rois_debug()."""
        return (self.name, self.x_pct, self.y_pct, self.w_pct, self.h_pct, self.roi_type.value)


# ============================================================================
# Perfis de ROI — coordenadas calibradas por observação das imagens
# ============================================================================

def get_hologic_rois() -> list[ROI]:
    """ROIs calibrados para relatórios Hologic Horizon (fundo cinza).

    Layout observado (debug images):

    ┌─────────────────────────────────────────┐  0%
    │  CABEÇALHO (fundo cinza, clínica)       │
    ├──────────────┬──────────────┬───────────┤  ~7%
    │              │              │           │
    │  INFO        │  IMAGEM      │  GRÁFICO  │
    │  PACIENTE    │  ANATÔMICA   │  T-score  │
    │  (esq 33%)   │  (23%)       │  (dir 43%)│
    │              │              │           │
    ├──────────────┼──────────────┴───────────┤  ~45%
    │  (cont.)     │  TÍTULO DO SITE          │
    │              ├──────────────────────────┤  ~51%
    │              │  CABEÇALHO TABELA        │
    │              ├──────────────────────────┤  ~56%
    │              │  DADOS DA TABELA         │
    │              │  (Região, Área, CMO, DMO,│
    │              │   Escore T, PR, Escore Z)│
    ├──────────────┴──────────────────────────┤  ~71%
    │  COMENTÁRIOS                            │
    ├─────────────────────────────────────────┤  ~80%
    │  RODAPÉ HOLOGIC                         │
    └─────────────────────────────────────────┘ 100%
    """
    return [
        ROI("header",       0.00, 0.00, 1.00, 0.07, ROIType.HEADER),
        ROI("patient_info", 0.00, 0.07, 0.33, 0.55, ROIType.PATIENT_INFO),
        ROI("anatomical",   0.33, 0.07, 0.24, 0.38, ROIType.ANATOMICAL),
        ROI("graph",        0.57, 0.07, 0.43, 0.38, ROIType.GRAPH),
        ROI("site_title",   0.31, 0.45, 0.69, 0.06, ROIType.TABLE_HEADER),
        ROI("table_header", 0.31, 0.51, 0.69, 0.05, ROIType.TABLE_HEADER),
        ROI("table_data",   0.30, 0.56, 0.70, 0.22, ROIType.TABLE_DATA),
        ROI("comments",     0.00, 0.71, 1.00, 0.09, ROIType.COMMENTS),
        ROI("footer",       0.00, 0.80, 1.00, 0.20, ROIType.FOOTER),
    ]


def detect_manufacturer(img: np.ndarray) -> str:
    """Detecta fabricante do equipamento DXA pela análise do fundo do cabeçalho.

    Amostra a faixa superior direita (sem scan), onde GE tem fundo branco (~230+)
    e Hologic tem fundo cinza (~128-170).

    Returns
    -------
    str
        'hologic' ou 'ge'
    """
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]

    # Região 1: canto superior direito (topo 6%, direita 40%).
    # GE Lunar DPX: fundo branco (~230). Hologic Horizon: fundo cinza (~128-170).
    # Afastado do scan image e de logos que podem escurecer o centro.
    sample_header = float(np.mean(gray[int(h * 0.01):int(h * 0.06), int(w * 0.60):]))

    # Região 2: coluna esquerda do meio da página (10%-45%, esquerda 30%).
    # Hologic tem sidebar de informação do paciente com fundo cinza (~140-175).
    # GE tem fundo branco nessa área (~200+). Ajuda a discriminar quando o
    # cabeçalho GE está levemente acinzentado por compressão JPEG.
    sample_left = float(np.mean(gray[int(h * 0.10):int(h * 0.45), :int(w * 0.30)]))

    # Hologic: header cinza (<165) E barra lateral cinza (<188).
    # Ambas as condições evitam falsos-positivos por variação de iluminação isolada.
    is_hologic = sample_header < 165 and sample_left < 188
    manufacturer = "hologic" if is_hologic else "ge"
    log.debug(
        "detect_manufacturer: header=%.1f left=%.1f -> %s",
        sample_header, sample_left, manufacturer,
    )
    return manufacturer


def get_default_rois() -> list[ROI]:
    """Retorna ROIs padrão calibrados para relatórios GE Lunar DPX.

    Layout observado (ambas as páginas — coluna e fêmur — têm layout idêntico):

    ┌─────────────────────────────────────────┐  0%
    │  CABEÇALHO (nome da clínica, endereço)  │  
    ├─────────────────────────────────────────┤  ~7%
    │  DADOS DO PACIENTE (nome, data, etc.)   │  
    ├─────────────┬───────────────────────────┤  ~17%
    │             │  TÍTULO DO SITE + REF.    │  
    │  IMAGEM     ├───────────────────────────┤  ~22%
    │  ANATÔMICA  │  GRÁFICO DE REFERÊNCIA    │
    │  (scan)     │  (T-score vs idade)       │
    │             │                           │
    ├─────────────┼───────────────────────────┤  ~50%
    │  (cont.)    │  CABEÇALHO DA TABELA      │
    │             ├───────────────────────────┤  ~53%
    │             │  TABELA DE DADOS          │
    │             │  (BMD, T-score, Z-score)  │
    ├─────────────┴───────────────────────────┤  ~68%
    │  COMENTÁRIOS                            │
    ├─────────────────────────────────────────┤  ~75%
    │  NOTAS DE RODAPÉ (definições OMS)       │
    ├─────────────────────────────────────────┤  ~93%
    │  RODAPÉ (logo GE)                       │
    └─────────────────────────────────────────┘ 100%
    """
    return [
        ROI("header",          0.00, 0.00, 1.00, 0.07, ROIType.HEADER),
        ROI("patient_info",    0.00, 0.07, 1.00, 0.10, ROIType.PATIENT_INFO),
        ROI("anatomical",      0.00, 0.17, 0.42, 0.42, ROIType.ANATOMICAL),
        ROI("site_title",      0.42, 0.17, 0.58, 0.05, ROIType.TABLE_HEADER),
        ROI("graph",           0.42, 0.22, 0.58, 0.22, ROIType.GRAPH),
        # table_header/table_data start at x=0.43 — one pixel beyond the anatomical ROI
        # (x=0.42) to prevent the scan image from contaminating the contour projection.
        # y=0.44 starts right below the graph (ends ~0.44); h=0.32 provides ample
        # downward coverage for studies with up to 11 data rows.
        ROI("table_header",    0.43, 0.44, 0.57, 0.05, ROIType.TABLE_HEADER),
        ROI("table_data",      0.43, 0.44, 0.57, 0.32, ROIType.TABLE_DATA),
        ROI("comments",        0.00, 0.70, 1.00, 0.06, ROIType.COMMENTS),
        ROI("footnotes",       0.00, 0.76, 1.00, 0.17, ROIType.FOOTNOTES),
        ROI("footer",          0.00, 0.93, 1.00, 0.07, ROIType.FOOTER),
    ]


# ============================================================================
# Classificação de página
# ============================================================================

def classify_page(img: np.ndarray) -> PageType:
    """Classifica uma página como relatório ou scan puro.

    Heurística: analisa a faixa superior da imagem (~8%).
    - Relatórios DXA têm texto preto sobre fundo claro no topo (endereço da clínica).
    - Scans puros tendem a ser predominantemente escuros ou coloridos (imagem anatômica).

    Também verifica se há a faixa de dados do paciente (~8-17%) com contraste alto.
    """
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]

    # Analisar faixa do cabeçalho (topo 8%)
    header_strip = gray[0:int(h * 0.08), :]
    # Analisar faixa do paciente (8%-17%)
    patient_strip = gray[int(h * 0.08):int(h * 0.17), :]

    # Em páginas de relatório, o cabeçalho tem fundo claro (>200) com texto escuro
    header_mean = float(np.mean(header_strip))
    patient_mean = float(np.mean(patient_strip))

    # Calcular variância — páginas de relatório têm alta variância (texto + fundo)
    header_std = float(np.std(header_strip))
    patient_std = float(np.std(patient_strip))

    log.debug(
        "classify_page: header_mean=%.1f header_std=%.1f patient_mean=%.1f patient_std=%.1f",
        header_mean, header_std, patient_mean, patient_std,
    )

    # Relatório: fundo claro no topo com alguma variância (texto).
    # GE Lunar DPX: fundo branco (~230). Hologic Horizon: fundo cinza (~128-170).
    # Threshold reduzido para cobrir ambos os fabricantes.
    is_report = (
        header_mean > 100
        and (header_std > 15 or patient_std > 20)
    )

    if is_report:
        log.info("Página classificada como REPORT_PAGE")
        return PageType.REPORT_PAGE

    # Se o fundo for predominantemente escuro, é scan
    if header_mean < 100:
        log.info("Página classificada como SCAN_ONLY (fundo escuro)")
        return PageType.SCAN_ONLY

    log.info("Página classificada como UNKNOWN")
    return PageType.UNKNOWN


# ============================================================================
# Extração de ROIs
# ============================================================================

def extract_rois(img: np.ndarray, rois: list[ROI] | None = None) -> dict[str, np.ndarray]:
    """Recorta a imagem nas ROIs definidas.

    Parameters
    ----------
    img : np.ndarray
        Imagem completa da página.
    rois : list[ROI] | None
        Lista de ROIs. Se None, usa o perfil padrão GE Lunar DPX.

    Returns
    -------
    dict[str, np.ndarray]
        Mapeamento nome → sub-imagem recortada.
        Inclui apenas ROIs com should_ocr=True.
    """
    if rois is None:
        rois = get_default_rois()

    extracted: dict[str, np.ndarray] = {}
    for roi in rois:
        if not roi.should_ocr:
            log.debug("Pulando ROI '%s' (tipo: %s)", roi.name, roi.roi_type.value)
            continue

        cropped = crop_roi(img, roi.x_pct, roi.y_pct, roi.w_pct, roi.h_pct)
        if cropped.size == 0:
            log.warning("ROI '%s' resultou em imagem vazia — ignorando", roi.name)
            continue

        extracted[roi.name] = cropped
        log.debug("ROI '%s' extraída: %dx%d", roi.name, cropped.shape[1], cropped.shape[0])

    return extracted


# ============================================================================
# Refinamento dinâmico de ROIs por perfil de projeção horizontal
# ============================================================================

def _refine_table_roi_vertical(
    img: np.ndarray,
    roi: ROI,
    pad_top_pct: float = 0.02,
    pad_bottom_pct: float = 0.07,
    dark_density_threshold: float = 0.012,
    gap_close_px: int = 12,
    row_margin_pct: float = 0.005,
    max_shift_pct: float = 0.10,
    min_h_pct: float = 0.13,
) -> ROI:
    """Refina os limites verticais de uma ROI de tabela via perfil de projeção.

    Estratégia:
    1. Expande a janela de busca em torno da ROI fixa (pad_top/pad_bottom).
    2. Dentro dessa janela, computa o perfil de projeção horizontal:
       para cada linha Y, calcula a fração de pixels escuros (texto).
    3. Linhas com densidade de texto acima do limiar são marcadas como "text row".
    4. Aplica closing morfológico 1-D para fechar o espaçamento entre linhas
       da tabela (3-8 px) sem fundir regiões separadas por grandes vazios.
    5. Determina o primeiro e último "text row" — esse é o span real da tabela.
    6. Converte de volta a percentuais de página e valida que o deslocamento não
       seja suspeitamente grande (fallback para ROI fixa se for).

    Parameters
    ----------
    pad_top_pct : float
        Expansão da janela acima da ROI fixa (padrão reduzido a 2% para evitar
        capturar o gráfico de referência que termina logo acima da tabela).
    pad_bottom_pct : float
        Expansão da janela abaixo da ROI fixa.
    min_h_pct : float
        Altura mínima da ROI refinada como fração da página. Se o conteúdo
        detectado for mais estreito que isso (ex.: apenas os labels do eixo X
        do gráfico foram capturados), mantém a ROI fixa. Padrão 13% ≈ ~5 rows.
    max_shift_pct : float
        Deslocamento máximo tolerado entre a ROI fixa e a refinada. Se exceder,
        mantém a ROI fixa (guarda contra falsos positivos em páginas atípicas).
    """
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h_img, w_img = gray.shape[:2]

    # Janela de busca — ROI fixa + padding vertical
    y_search_start = max(0.0, roi.y_pct - pad_top_pct)
    y_search_end = min(1.0, roi.y_pct + roi.h_pct + pad_bottom_pct)

    y0 = int(y_search_start * h_img)
    y1 = int(y_search_end * h_img)
    x0 = int(roi.x_pct * w_img)
    x1 = int((roi.x_pct + roi.w_pct) * w_img)

    if y1 <= y0 or x1 <= x0:
        return roi

    strip = gray[y0:y1, x0:x1]

    # Binarizar invertido: pixels escuros (texto) → 255
    _, bw = cv2.threshold(strip, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # Perfil de projeção horizontal: fração de pixels "texto" por linha
    row_dark_fraction = np.sum(bw, axis=1).astype(float) / (bw.shape[1] * 255)

    # Máscara booleana de linhas com texto
    text_mask = (row_dark_fraction > dark_density_threshold).astype(np.uint8).reshape(-1, 1)

    if int(text_mask.sum()) == 0:
        log.debug("ROI '%s' refinement: nenhuma linha de texto detectada — mantendo fixa", roi.name)
        return roi

    # Closing morfológico 1-D para fechar espaços entre linhas da tabela
    if gap_close_px > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, gap_close_px))
        text_mask = cv2.morphologyEx(text_mask, cv2.MORPH_CLOSE, kernel)

    text_indices = np.where(text_mask.flatten())[0]
    if len(text_indices) == 0:
        return roi

    first_row = int(text_indices[0])
    last_row = int(text_indices[-1])

    # Converter linhas do strip para coordenadas absolutas da página
    margin_px = max(2, int(row_margin_pct * h_img))
    first_px = max(0, y0 + first_row - margin_px)
    last_px = min(h_img - 1, y0 + last_row + margin_px)

    new_y_pct = first_px / h_img
    new_h_pct = (last_px - first_px) / h_img

    if new_h_pct <= 0:
        return roi

    # Guard: recusar refinamento se o span detectado for muito estreito.
    # Isso previne que labels de eixo do gráfico (poucas linhas) substituam
    # a ROI fixa que cobre a tabela inteira.
    if new_h_pct < min_h_pct:
        log.debug(
            "ROI '%s' refinement: new_h_pct=%.3f < min_h_pct=%.3f — mantendo fixa",
            roi.name, new_h_pct, min_h_pct,
        )
        return roi

    # Validação: rejeitar se o deslocamento for suspeito (ruído, página atípica)
    y_shift = abs(new_y_pct - roi.y_pct)
    if y_shift > max_shift_pct:
        log.debug(
            "ROI '%s' refinement: y_shift=%.3f excede max=%.3f — mantendo fixa",
            roi.name, y_shift, max_shift_pct,
        )
        return roi

    log.debug(
        "ROI '%s' refinada: y %.3f→%.3f  h %.3f→%.3f  (shift=%.3f)",
        roi.name, roi.y_pct, new_y_pct, roi.h_pct, new_h_pct, y_shift,
    )

    return ROI(
        name=roi.name,
        x_pct=roi.x_pct,
        y_pct=new_y_pct,
        w_pct=roi.w_pct,
        h_pct=new_h_pct,
        roi_type=roi.roi_type,
    )


def refine_rois(img: np.ndarray, rois: list[ROI], strategy: str = "fixed") -> list[ROI]:
    """Refina limites verticais das ROIs de tabela com base na estratégia configurada.

    Parameters
    ----------
    strategy : str
        ``'fixed'``   — sem refinamento, retorna ROIs inalteradas (padrão).
        ``'contour'`` — refina ``table_data`` e ``table_header`` via projeção.
        ``'hybrid'``  — mesmo que ``'contour'`` (alias para extensão futura).

    O refinamento detecta dinamicamente onde o conteúdo da tabela começa e
    termina dentro de uma janela em torno da ROI fixa, tornando o pipeline
    robusto a deslocamentos verticais de ±~10% da altura da página.
    """
    if strategy == "fixed":
        return rois

    refined: list[ROI] = []
    for roi in rois:
        if roi.name in ("table_data", "table_header"):
            refined.append(_refine_table_roi_vertical(img, roi))
        else:
            refined.append(roi)
    return refined
