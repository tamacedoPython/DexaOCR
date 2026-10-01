"""Pipeline principal do DexaOCR — orquestração baseada em ROI.

Fluxo:
  1. Buscar DICOMs (banco ou arquivo local)
  2. Converter DICOM → PNG
  3. Para cada PNG:
     a. Classificar página (report vs scan)
     b. Extrair ROIs (pular gráficos e imagens anatômicas)
     c. Pré-processar cada ROI com pipeline específico
     d. Executar OCR com config específico por tipo de ROI
  4. Parsear dados de todas as páginas de relatório
  5. Montar DxaReport e exportar
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

from .config import get_settings, Settings
from .db import DBConnectionManager
from .dicom_locator import get_dicom_paths_by_study_uid
from .dicom_to_png import dicom_to_pil, save_png
from .models.dxa_models import DxaReport, OCRMetadata, SiteResult
from .services.roi_detector import (
    classify_page, PageType, extract_rois, get_default_rois,
    detect_manufacturer, get_hologic_rois, refine_rois,
)
from .services.preprocessing import (
    preprocess_for_header,
    preprocess_for_table,
    preprocess_for_table_hologic,
    preprocess_for_comments,
    preprocess_for_footnotes,
)
from .services.ocr_engine import create_engine, get_ocr_params, OCREngine
from .services.parser_header import parse_patient_info
from .services.parser_table import build_site_result
from .services.parser_comments import parse_comments
from .services.exporter import save_outputs
from .utils.logger import setup_logging, get_logger
from .utils.image_utils import draw_rois_debug, save_debug_image


# ============================================================================
# CLI
# ============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OCR offline para estudos DXA — pipeline baseado em ROI")
    parser.add_argument("--study-uid", help="StudyUID do exame no PACS/banco")
    parser.add_argument("--dicom-file", help="Caminho de um DICOM único")
    parser.add_argument("--output-dir", help="Pasta de saída")
    parser.add_argument("--max-images", type=int, default=20, help="Máximo de imagens a processar")
    parser.add_argument("--debug", action="store_true", help="Salvar imagens intermediárias e logs detalhados")
    parser.add_argument("--engine", choices=["tesseract", "paddleocr"], help="Engine OCR a usar")
    return parser


# ============================================================================
# Conversão DICOM → PNG (preservada do pipeline original)
# ============================================================================

def export_dicoms_to_png(
    dicom_paths: list[Path],
    output_dir: Path,
    resize_width: int,
    compress_level: int,
) -> list[Path]:
    """Converte DICOMs para PNG, retornando lista de caminhos gerados."""
    png_files: list[Path] = []
    for idx, dicom_path in enumerate(dicom_paths, start=1):
        img = dicom_to_pil(dicom_path, resize_width=resize_width)
        png_path = output_dir / f"image_{idx:03d}.png"
        save_png(img, png_path, compress_level=compress_level)
        png_files.append(png_path)
    return png_files


# ============================================================================
# Helpers de parsing
# ============================================================================

def _merge_site_result(existing: SiteResult, new: SiteResult) -> None:
    """Mescla linhas de *new* em *existing*, preenchendo campos None.

    Quando o mesmo sítio anatômico aparece em duas páginas do estudo (ex.:
    DICOM de scan + DICOM de relatório para o mesmo fêmur), a segunda página
    pode complementar campos que a primeira não capturou. A estratégia
    'first-wins' para cada campo garante que dados já presentes não são
    sobrescritos por OCR possivelmente pior da segunda página.
    """
    existing_map = {row.region: row for row in existing.rows}
    for new_row in new.rows:
        if new_row.region in existing_map:
            old = existing_map[new_row.region]
            if old.bmd is None:                old.bmd = new_row.bmd
            if old.t_score is None:            old.t_score = new_row.t_score
            if old.z_score is None:            old.z_score = new_row.z_score
            if old.young_adult_percent is None:  old.young_adult_percent = new_row.young_adult_percent
            if old.age_matched_percent is None:  old.age_matched_percent = new_row.age_matched_percent
        else:
            existing.rows.append(new_row)
            existing_map[new_row.region] = new_row


# ============================================================================
# Mapeamento ROI → preprocessamento
# ============================================================================

_PREPROCESS_MAP = {
    "header": preprocess_for_header,
    "patient_info": preprocess_for_header,
    "scan_info": preprocess_for_header,
    "site_title": preprocess_for_header,
    "table_header": preprocess_for_table,
    "table_data": preprocess_for_table,
    "comments": preprocess_for_comments,
    "footnotes": preprocess_for_footnotes,
}

_PREPROCESS_MAP_HOLOGIC = {
    **_PREPROCESS_MAP,
    "table_data": preprocess_for_table_hologic,
    "table_header": preprocess_for_table_hologic,
}


# ============================================================================
# Processamento de uma página de relatório
# ============================================================================

def process_report_page(
    img: np.ndarray,
    page_name: str,
    engine: OCREngine,
    settings: Settings,
    output_dir: Path,
    debug: bool,
) -> dict[str, str]:
    """Processa uma página de relatório: ROI → preprocess → OCR.

    Returns
    -------
    dict[str, str]
        Mapeamento roi_name → texto OCR extraído.
    """
    log = get_logger("pipeline")
    manufacturer = detect_manufacturer(img)
    rois = get_hologic_rois() if manufacturer == "hologic" else get_default_rois()
    rois = refine_rois(img, rois, strategy=settings.roi_strategy)
    preprocess_map = _PREPROCESS_MAP_HOLOGIC if manufacturer == "hologic" else _PREPROCESS_MAP
    log.info("Fabricante detectado: %s", manufacturer)

    # Salvar imagem de debug com ROIs desenhadas
    if debug:
        debug_dir = output_dir / "debug"
        debug_img = draw_rois_debug(img, [r.as_tuple() for r in rois])
        save_debug_image(debug_img, debug_dir / f"{page_name}_rois.png")

    # Extrair ROIs (pula anatomical, graph, footer automaticamente)
    roi_images = extract_rois(img, rois)

    texts: dict[str, str] = {}
    for roi_name, roi_img in roi_images.items():
        # Pré-processar com pipeline específico ao fabricante
        preprocess_fn = preprocess_map.get(roi_name, preprocess_for_header)
        processed = preprocess_fn(roi_img)

        # Salvar imagem pré-processada em debug
        if debug:
            debug_dir = output_dir / "debug"
            save_debug_image(roi_img, debug_dir / f"{page_name}_{roi_name}_raw.png")
            save_debug_image(processed, debug_dir / f"{page_name}_{roi_name}_processed.png")

        # OCR com config específico
        ocr_params = get_ocr_params(roi_name, settings)
        text = engine.recognize(processed, **ocr_params)
        texts[roi_name] = text

        if text:
            log.debug("OCR [%s/%s]: %d chars", page_name, roi_name, len(text))
        else:
            log.debug("OCR [%s/%s]: vazio", page_name, roi_name)

    # Preservar fabricante para uso no parsing
    texts["_manufacturer"] = manufacturer
    return texts


# ============================================================================
# Pipeline principal
# ============================================================================

def main() -> int:
    args = build_parser().parse_args()
    settings = get_settings()

    # Override engine via CLI
    if args.engine:
        # Sobrescrever a engine configurada via .env
        object.__setattr__(settings, 'ocr_engine', args.engine)

    # Override debug via CLI
    debug = args.debug or settings.save_debug_images

    # Logging
    log_level = "DEBUG" if debug else settings.log_level
    setup_logging(level=log_level, log_file=settings.log_file)
    log = get_logger("pipeline")

    output_dir = Path(args.output_dir) if args.output_dir else settings.default_output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    if not args.study_uid and not args.dicom_file:
        raise SystemExit("Informe --study-uid ou --dicom-file.")

    # --- Buscar DICOMs ---
    dicom_paths: list[Path]
    if args.dicom_file:
        dicom_paths = [Path(args.dicom_file)]
    else:
        db = DBConnectionManager()
        with db.connect(settings) as conn:
            dicom_paths = get_dicom_paths_by_study_uid(conn, args.study_uid, dicom_roots=settings.dicom_roots)

    if not dicom_paths:
        raise SystemExit("Nenhum DICOM encontrado para o critério informado.")

    dicom_paths = dicom_paths[: args.max_images]
    log.info("DICOMs encontrados: %d", len(dicom_paths))

    # --- Converter DICOM → PNG ---
    png_files = export_dicoms_to_png(
        dicom_paths=dicom_paths,
        output_dir=output_dir,
        resize_width=settings.default_resize_width,
        compress_level=settings.png_compress_level,
    )
    log.info("PNGs gerados: %d", len(png_files))

    # --- Criar engine OCR ---
    engine = create_engine(settings)
    log.info("Engine OCR: %s", engine.name)

    # --- Processar cada página ---
    metadata = OCRMetadata(engine=engine.name)
    all_raw_texts: dict[str, dict[str, str]] = {}
    report_pages_data: list[dict[str, str]] = []

    for png_path in png_files:
        page_name = png_path.stem
        img = cv2.imread(str(png_path))

        if img is None:
            log.error("Não foi possível ler: %s", png_path)
            metadata.warnings.append(f"Falha ao ler {png_path.name}")
            continue

        # Classificar página
        page_type = classify_page(img)

        if page_type == PageType.SCAN_ONLY:
            log.info("Página %s: SCAN_ONLY — ignorada", page_name)
            metadata.pages_skipped += 1
            continue

        if page_type == PageType.UNKNOWN:
            log.warning("Página %s: UNKNOWN — tentando processar mesmo assim", page_name)
            metadata.warnings.append(f"{page_name}: tipo desconhecido, processado com ressalvas")

        # Processar ROIs
        texts = process_report_page(img, page_name, engine, settings, output_dir, debug)
        all_raw_texts[page_name] = texts
        report_pages_data.append(texts)
        metadata.pages_processed += 1

    if not report_pages_data:
        log.error("Nenhuma página de relatório encontrada!")
        metadata.warnings.append("Nenhuma página de relatório identificada")
        # Salvar output mínimo mesmo sem dados
        report = DxaReport(ocr_metadata=metadata)
        save_outputs(report, output_dir, all_raw_texts)
        return 1

    # --- Parsear dados ---

    # Paciente: usar a primeira página de relatório
    first_page = report_pages_data[0]
    patient_info = parse_patient_info(
        header_text=first_page.get("header", "") + "\n" + first_page.get("patient_info", ""),
        patient_text=first_page.get("patient_info", ""),
    )

    # Sites: cada página de relatório pode ter um sítio anatômico diferente
    sites = []
    all_comments: list[str] = []

    for page_texts in report_pages_data:
        manufacturer_hint = page_texts.get("_manufacturer", "auto")
        site_result = build_site_result(
            site_title_text=page_texts.get("site_title", ""),
            table_header_text=page_texts.get("table_header", ""),
            table_data_text=page_texts.get("table_data", ""),
            table_format_hint=manufacturer_hint,
        )
        if site_result:
            existing = next((s for s in sites if s.site_type == site_result.site_type), None)
            if existing:
                _merge_site_result(existing, site_result)
                log.info(
                    "Site '%s' duplicado na página — mesclando %d linhas",
                    site_result.site_type, len(site_result.rows),
                )
            else:
                sites.append(site_result)
        else:
            metadata.warnings.append("Não foi possível extrair dados de uma página")

        # Comentários
        comment_text = parse_comments(page_texts.get("comments", ""))
        if comment_text:
            all_comments.append(comment_text)

    # --- Montar relatório ---
    report = DxaReport(
        patient_info=patient_info,
        sites=sites,
        comments="\n\n".join(all_comments),
        ocr_metadata=metadata,
    )

    # --- Exportar ---
    save_outputs(report, output_dir, all_raw_texts)

    # --- Resumo ---
    print(f"DICOMs processados: {len(dicom_paths)}")
    print(f"Páginas de relatório: {metadata.pages_processed}")
    print(f"Páginas ignoradas (scan): {metadata.pages_skipped}")
    print(f"Sites extraídos: {len(sites)}")
    for site in sites:
        print(f"  - {site.site_name}: {len(site.rows)} linhas")
    print(f"Engine OCR: {engine.name}")
    print(f"Saída em: {output_dir.resolve()}")
    if metadata.warnings:
        print(f"Avisos: {len(metadata.warnings)}")
        for w in metadata.warnings:
            print(f"  [!] {w}")

    return 0

