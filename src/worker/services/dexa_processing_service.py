"""
DexaOCR Processing Service — adapter between the Worker infrastructure and
the existing src/dexa_ocr pipeline.

Responsibilities:
- Create an isolated working directory per request (work/{request_id}/)
- Drive the existing pipeline (locate DICOM → convert → OCR → parse → export)
- Return structured data dict and OCR metadata
- Clean up temporary files
- Never import messaging or repository — pure domain logic
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

import cv2

from ...dexa_ocr.config import Settings
from ...dexa_ocr.db import DBConnectionManager
from ...dexa_ocr.dicom_locator import get_dicom_paths_by_study_uid
from ...dexa_ocr.dicom_to_png import dicom_to_pil, save_png
from ...dexa_ocr.models.dxa_models import DxaReport, OCRMetadata
from ...dexa_ocr.pipeline import export_dicoms_to_png, process_report_page
from ...dexa_ocr.services.exporter import save_outputs
from ...dexa_ocr.services.ocr_engine import create_engine
from ...dexa_ocr.services.parser_comments import parse_comments
from ...dexa_ocr.services.parser_header import parse_patient_info
from ...dexa_ocr.services.parser_table import build_site_result
from ...dexa_ocr.services.roi_detector import PageType, classify_page
from ..config.settings import WorkerSettings

logger = logging.getLogger("worker.services.dexa_processing")


class DexaProcessingError(Exception):
    """Raised when the DexaOCR pipeline fails to produce usable output."""


def _is_result_sufficient(report: DxaReport) -> bool:
    """Return True when the report contains both key measurements:

    - A lumbar-spine L1-L4 total row (or "Total" in Hologic) with a BMD value
    - A femoral-neck "Colo" row with a BMD value in at least one femur site

    Either absence triggers a retry when ``OCR_MAX_RETRIES > 0``.
    """
    has_lumbar_total = False
    has_femoral_neck = False

    for site in report.sites:
        if site.site_type == "lumbar_spine":
            for row in site.rows:
                # Covers "L1-L4", "L1–L4", "L1 - L4" and "Total" (Hologic)
                if re.match(r"^L1\s*[-\u2013]\s*L4$|^Total$", row.region.strip(), re.IGNORECASE):
                    if row.bmd is not None:
                        has_lumbar_total = True
                        break
        elif site.site_type in ("right_femur", "left_femur"):
            for row in site.rows:
                if row.region.strip().lower() == "colo" and row.bmd is not None:
                    has_femoral_neck = True
                    break

    return has_lumbar_total and has_femoral_neck


class DexaOCRProcessingService:
    """
    Adapter that runs the existing DexaOCR pipeline for a single study.

    Usage:
        service = DexaOCRProcessingService(worker_settings)
        structured_data, ocr_metadata = service.process(study_uid, request_id)
    """

    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._ocr_settings = self._build_ocr_settings(settings)

    @staticmethod
    def _build_ocr_settings(worker_settings: WorkerSettings) -> Settings:
        """
        Build a dexa_ocr Settings object from WorkerSettings.
        Constructs the dataclass directly so worker config is authoritative.
        """
        from pathlib import Path as _Path
        from ...dexa_ocr.config import Settings as OcrSettings
        import os

        log_file_raw = os.getenv("LOG_FILE")
        return OcrSettings(
            sql_hosts=worker_settings.sql_hosts,
            sql_db=worker_settings.sql_db,
            sql_uid=worker_settings.sql_uid,
            sql_pwd=worker_settings.sql_pwd,
            sql_port=worker_settings.sql_port,
            sql_encrypt=worker_settings.sql_encrypt,
            sql_trust_cert=worker_settings.sql_trust_cert,
            odbc_driver=worker_settings.odbc_driver,
            tesseract_cmd=worker_settings.tesseract_cmd,
            tesseract_lang=worker_settings.tesseract_lang,
            ocr_engine=worker_settings.ocr_engine,
            paddleocr_lang=worker_settings.paddleocr_lang,
            ocr_table_whitelist=os.getenv(
                "OCR_TABLE_WHITELIST", "0123456789.,-LlTtWwCcDd aboréáíú"
            ),
            default_resize_width=int(os.getenv("DEFAULT_RESIZE_WIDTH", "2200")),
            png_compress_level=int(os.getenv("PNG_COMPRESS_LEVEL", "3")),
            roi_strategy=os.getenv("ROI_STRATEGY", "contour"),
            save_debug_images=worker_settings.save_debug_images,
            log_level=worker_settings.log_level,
            log_file=_Path(log_file_raw) if log_file_raw else None,
            default_output_dir=worker_settings.work_base_dir,
            dicom_roots=worker_settings.dicom_roots,
        )

    def _make_work_dir(self, request_id: str) -> Path:
        """Create an isolated work directory for this request."""
        work_dir = self._settings.work_base_dir / request_id
        work_dir.mkdir(parents=True, exist_ok=True)
        return work_dir

    def _cleanup_work_dir(self, work_dir: Path) -> None:
        """Remove temporary files after processing."""
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
            logger.debug("Cleaned up work dir: %s", work_dir)
        except Exception as exc:
            logger.warning("Failed to clean work dir %s: %s", work_dir, exc)

    def _run_pipeline(self, study_uid: str, work_dir: Path, debug: bool) -> DxaReport:
        """Execute the full dexa_ocr pipeline and return a DxaReport."""
        settings = self._ocr_settings

        # 1. Locate DICOMs
        db = DBConnectionManager()
        with db.connect(settings) as conn:
            dicom_paths = get_dicom_paths_by_study_uid(conn, study_uid, dicom_roots=settings.dicom_roots)

        if not dicom_paths:
            raise DexaProcessingError(f"No DICOM files found for study_uid={study_uid}")

        logger.info("Found %d DICOM file(s) for study_uid=%s", len(dicom_paths), study_uid)

        # 2. Convert to PNG
        png_files = export_dicoms_to_png(
            dicom_paths=dicom_paths,
            output_dir=work_dir,
            resize_width=settings.default_resize_width,
            compress_level=settings.png_compress_level,
        )

        if not png_files:
            raise DexaProcessingError(
                f"DICOM conversion produced no PNG files for study_uid={study_uid}"
            )

        # 3. OCR engine
        engine = create_engine(settings)
        logger.info("OCR engine: %s", engine.name)

        # 4. Process pages
        metadata = OCRMetadata(engine=engine.name)
        all_raw_texts: Dict[str, Dict[str, str]] = {}
        report_pages_data: List[Dict[str, str]] = []

        for png_path in png_files:
            page_name = png_path.stem
            img = cv2.imread(str(png_path))
            if img is None:
                logger.error("Could not read image: %s", png_path)
                metadata.warnings.append(f"Failed to read {png_path.name}")
                continue

            page_type = classify_page(img)

            if page_type == PageType.SCAN_ONLY:
                logger.info("Page %s: SCAN_ONLY — skipped", page_name)
                metadata.pages_skipped += 1
                continue

            if page_type == PageType.UNKNOWN:
                logger.warning("Page %s: UNKNOWN type — processing anyway", page_name)
                metadata.warnings.append(f"{page_name}: unknown page type, processed with caveats")

            texts = process_report_page(img, page_name, engine, settings, work_dir, debug)
            all_raw_texts[page_name] = texts
            report_pages_data.append(texts)
            metadata.pages_processed += 1

        if not report_pages_data:
            metadata.warnings.append("No report pages identified")
            return DxaReport(ocr_metadata=metadata)

        # 5. Parse
        first_page = report_pages_data[0]
        patient_info = parse_patient_info(
            header_text=first_page.get("header", "") + "\n" + first_page.get("patient_info", ""),
            patient_text=first_page.get("patient_info", ""),
        )

        sites = []
        all_comments: List[str] = []

        for page_texts in report_pages_data:
            metadata.warnings.extend(page_texts.get("_table_warnings", "").splitlines())
            manufacturer_hint = page_texts.get("_manufacturer", "auto")
            site_result = build_site_result(
                site_title_text=page_texts.get("site_title", ""),
                table_header_text=page_texts.get("table_header", ""),
                table_data_text=page_texts.get("table_data", ""),
                table_format_hint=page_texts.get("_table_format", manufacturer_hint),
            )
            if site_result:
                existing = next((s for s in sites if s.site_type == site_result.site_type), None)
                if existing:
                    from ...dexa_ocr.pipeline import _merge_site_result
                    _merge_site_result(existing, site_result)
                else:
                    sites.append(site_result)
            else:
                metadata.warnings.append("Could not extract site data from one page")

            comment_text = parse_comments(page_texts.get("comments", ""))
            if comment_text:
                all_comments.append(comment_text)

        return DxaReport(
            patient_info=patient_info,
            sites=sites,
            comments="\n\n".join(all_comments),
            ocr_metadata=metadata,
        )

    def process(
        self,
        study_uid: str,
        request_id: str,
        *,
        return_debug_data: bool = False,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Run the full DexaOCR pipeline for a study.

        Returns:
            (structured_data, ocr_metadata)

        Raises:
            DexaProcessingError: on any failure in the pipeline.
        """
        work_dir = self._make_work_dir(request_id)
        logger.info(
            "DexaOCR processing started — study_uid=%s work_dir=%s",
            study_uid,
            work_dir,
        )

        try:
            max_retries = self._settings.ocr_max_retries
            report: DxaReport | None = None

            for attempt in range(1 + max_retries):
                report = self._run_pipeline(
                    study_uid=study_uid,
                    work_dir=work_dir,
                    debug=return_debug_data,
                )

                if _is_result_sufficient(report):
                    if attempt > 0:
                        logger.info(
                            "Attempt %d/%d succeeded with complete OCR data — study_uid=%s",
                            attempt + 1, 1 + max_retries, study_uid,
                        )
                    break

                if attempt < max_retries:
                    logger.warning(
                        "Attempt %d/%d: OCR result incomplete "
                        "(missing L1-L4 and/or Colo femoral) — retrying — study_uid=%s",
                        attempt + 1, 1 + max_retries, study_uid,
                    )
                else:
                    logger.warning(
                        "All %d attempt(s) exhausted with incomplete OCR data — study_uid=%s",
                        1 + max_retries, study_uid,
                    )
                    report.ocr_metadata.warnings.append(
                        f"OCR incomplete after {1 + max_retries} attempt(s): "
                        "missing L1-L4 and/or Colo femoral"
                    )

            # Serialize
            full_dict = report.to_dict()
            ocr_metadata = full_dict.pop("ocr_metadata", {})

            logger.info(
                "DexaOCR processing complete — study_uid=%s engine=%s pages_processed=%s",
                study_uid,
                ocr_metadata.get("engine"),
                ocr_metadata.get("pages_processed"),
            )

            return full_dict, ocr_metadata

        except DexaProcessingError:
            raise

        except Exception as exc:
            logger.error(
                "Unexpected error in DexaOCR pipeline for study_uid=%s: %s",
                study_uid,
                exc,
                exc_info=True,
            )
            raise DexaProcessingError(
                f"Pipeline failed for study_uid={study_uid}: {exc}"
            ) from exc

        finally:
            if not return_debug_data:
                self._cleanup_work_dir(work_dir)

