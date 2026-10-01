"""Pré-processamento de imagem para OCR de relatórios DXA.

Cada função recebe e retorna np.ndarray, permitindo composição flexível.
Os pipelines compostos no final combinam as etapas para cada tipo de ROI.
"""

from __future__ import annotations

import cv2
import numpy as np


# ============================================================================
# Funções atômicas — cada etapa é independente e testável
# ============================================================================

def to_grayscale(img: np.ndarray) -> np.ndarray:
    """Converte para escala de cinza. Se já for 2D, retorna cópia."""
    if img.ndim == 2:
        return img.copy()
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def enhance_contrast(gray: np.ndarray, clip_limit: float = 2.0, grid: tuple[int, int] = (8, 8)) -> np.ndarray:
    """Aplica CLAHE (Contrast Limited Adaptive Histogram Equalization).

    CLAHE é superior à equalização global para páginas com regiões mistas
    (texto + fundo variável), porque melhora o contraste localmente sem
    saturar regiões já bem definidas.
    """
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=grid)
    return clahe.apply(gray)


def adaptive_binarize(gray: np.ndarray, block_size: int = 31, C: int = 11) -> np.ndarray:
    """Binarização adaptativa (Gaussiana).

    Funciona melhor que threshold global em imagens com iluminação desigual,
    como sobreposições de texto em equipamentos DXA onde o fundo pode variar
    entre áreas da imagem.

    Parameters
    ----------
    block_size : int
        Tamanho do bloco para cálculo do limiar local (deve ser ímpar).
    C : int
        Constante subtraída da média local. Valores maiores = mais agressivo.
    """
    return cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        block_size, C,
    )


def global_binarize(gray: np.ndarray) -> np.ndarray:
    """Binarização com limiar automático (Otsu).

    Melhor para imagens com distribuição bimodal clara (texto preto sobre
    fundo branco uniforme). Usado como alternativa à binarização adaptativa
    quando o fundo é relativamente homogêneo.
    """
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return bw


def denoise(gray: np.ndarray, strength: int = 10) -> np.ndarray:
    """Remove ruído usando Non-Local Means Denoising.

    Preserva bordas (melhor que blur gaussiano para OCR) enquanto remove
    ruído de alta frequência que confunde o Tesseract.
    """
    return cv2.fastNlMeansDenoising(gray, None, h=strength, templateWindowSize=7, searchWindowSize=21)


def morphological_clean(bw: np.ndarray, kernel_size: int = 2) -> np.ndarray:
    """Operações morfológicas leves para remover pontos isolados.

    Aplica abertura (erosão → dilatação) que remove ruído pequeno
    sem afetar significativamente caracteres de texto.
    """
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_size, kernel_size))
    return cv2.morphologyEx(bw, cv2.MORPH_OPEN, kernel)


def deskew(img: np.ndarray, max_angle: float = 5.0) -> np.ndarray:
    """Corrige inclinação da imagem via detecção de linhas (Hough).

    Se o ângulo detectado for > max_angle, não aplica rotação (provavelmente
    é um falso positivo). A rotação usa fundo branco (255).
    """
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=100, minLineLength=100, maxLineGap=10)

    if lines is None:
        return img

    # Calcular ângulo mediano das linhas detectadas
    angles = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(angle) < max_angle:
            angles.append(angle)

    if not angles:
        return img

    median_angle = float(np.median(angles))
    if abs(median_angle) < 0.3:
        return img  # Não vale a pena rotacionar por menos de 0.3°

    h, w = img.shape[:2]
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, median_angle, 1.0)
    return cv2.warpAffine(img, matrix, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)


def resize_for_ocr(img: np.ndarray, target_width: int = 2200) -> np.ndarray:
    """Redimensiona mantendo proporção para a largura alvo.

    Imagens maiores geralmente produzem OCR melhor (até certo ponto).
    O padrão de 2200px funciona bem para relatórios DXA A4.
    """
    h, w = img.shape[:2]
    if w == target_width:
        return img
    scale = target_width / w
    new_h = int(h * scale)
    interpolation = cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA
    return cv2.resize(img, (target_width, new_h), interpolation=interpolation)


# ============================================================================
# Pipelines compostos — combinam etapas para cada tipo de ROI
# ============================================================================

def preprocess_for_header(img: np.ndarray) -> np.ndarray:
    """Pipeline para ROI de cabeçalho e dados do paciente.

    Foco em texto corrido com fontes médias. CLAHE + binarização adaptativa
    + denoise leve para limpar artefatos de compressão.
    """
    gray = to_grayscale(img)
    gray = resize_for_ocr(gray, target_width=2400)
    gray = enhance_contrast(gray, clip_limit=2.0)
    gray = denoise(gray, strength=8)
    bw = adaptive_binarize(gray, block_size=31, C=10)
    return bw


def preprocess_for_table(img: np.ndarray) -> np.ndarray:
    """Pipeline para ROI da tabela densitométrica GE Lunar.

    Foco em números pequenos dispostos em colunas. CLAHE leve antes do Otsu
    garante contraste uniforme mesmo em imagens com iluminação variável (common
    em DICOMs escaneados). Limpeza morfológica remove linhas de grade parciais.
    """
    gray = to_grayscale(img)
    # Ampliação maior para melhor leitura de números pequenos
    gray = resize_for_ocr(gray, target_width=2800)
    # CLAHE leve antes do Otsu: equaliza eventuais variações de iluminação
    # sem distorcer a distribuição bimodal texto/fundo que o Otsu espera
    gray = enhance_contrast(gray, clip_limit=2.0, grid=(8, 8))
    # Otsu binarization — works better for clean dark-on-light table text
    bw = global_binarize(gray)
    bw = morphological_clean(bw, kernel_size=2)
    return bw


def preprocess_for_comments(img: np.ndarray) -> np.ndarray:
    """Pipeline para ROI de comentários.

    Texto corrido menor que o cabeçalho. Denoise mais agressivo porque
    a região costuma ter artefatos de bordas próximas (gráficos, etc.).
    """
    gray = to_grayscale(img)
    gray = resize_for_ocr(gray, target_width=2400)
    gray = enhance_contrast(gray, clip_limit=2.0)
    gray = denoise(gray, strength=12)
    bw = adaptive_binarize(gray, block_size=25, C=10)
    return bw


def preprocess_for_footnotes(img: np.ndarray) -> np.ndarray:
    """Pipeline para ROI de notas de rodapé.

    Texto muito pequeno — precisa de ampliação agressiva e binarização
    cuidadosa para não perder detalhes finos dos caracteres.
    """
    gray = to_grayscale(img)
    # Ampliação maior porque o texto é muito pequeno
    gray = resize_for_ocr(gray, target_width=3200)
    gray = enhance_contrast(gray, clip_limit=2.5)
    gray = denoise(gray, strength=6)
    bw = adaptive_binarize(gray, block_size=21, C=8)
    return bw


def preprocess_for_table_hologic(img: np.ndarray) -> np.ndarray:
    """Pipeline para ROI da tabela densitométrica Hologic Horizon.

    O equipamento Hologic usa fundo cinza médio (~128) em vez do fundo branco
    do GE Lunar. A binarização global (Otsu) funciona bem no GE mas falha no
    Hologic porque o contraste texto/fundo é menor.

    Usa CLAHE mais agressivo para equalizar o fundo cinza antes da binarização
    adaptativa, que lida melhor com variações de iluminação locais.
    """
    gray = to_grayscale(img)
    gray = resize_for_ocr(gray, target_width=2800)
    # CLAHE mais agressivo para fundo cinza do Hologic
    gray = enhance_contrast(gray, clip_limit=3.5, grid=(6, 6))
    gray = denoise(gray, strength=8)
    # Binarização adaptativa em vez de global — lida melhor com fundo cinza variável
    bw = adaptive_binarize(gray, block_size=25, C=12)
    bw = morphological_clean(bw, kernel_size=2)
    return bw
