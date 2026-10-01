from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


@dataclass(slots=True)
class Settings:
    # --- Banco de dados ---
    sql_hosts: list[str]
    sql_db: str
    sql_uid: str
    sql_pwd: str
    sql_port: int
    sql_encrypt: str
    sql_trust_cert: str
    odbc_driver: str | None

    # --- OCR ---
    tesseract_cmd: str | None
    tesseract_lang: str
    ocr_engine: str              # "tesseract" ou "paddleocr"
    paddleocr_lang: str
    ocr_table_whitelist: str     # Whitelist de chars para OCR de tabela numérica

    # --- Imagem ---
    default_resize_width: int
    png_compress_level: int

    # --- ROI / Debug ---
    roi_strategy: str            # "fixed", "contour" ou "hybrid"
    save_debug_images: bool

    # --- Logging ---
    log_level: str
    log_file: Path | None

    # --- I/O ---
    default_output_dir: Path
    dicom_roots: list[str]  # Paths raiz dos DICOMs, tentados em ordem



def get_settings() -> Settings:
    hosts_raw = os.getenv("SQL_HOSTS", "192.168.1.211;201.48.134.100")
    hosts = [h.strip() for h in hosts_raw.split(";") if h.strip()]

    log_file_raw = os.getenv("LOG_FILE")

    return Settings(
        # Banco de dados
        sql_hosts=hosts,
        sql_db=os.getenv("SQL_DB", "AgileAI"),
        sql_uid=os.getenv("SQL_UID", "sasys"),
        sql_pwd=os.getenv("SQL_PWD", ""),
        sql_port=int(os.getenv("SQL_PORT", "1433")),
        sql_encrypt=os.getenv("SQL_ENCRYPT", "no"),
        sql_trust_cert=os.getenv("SQL_TRUST_CERT", "yes"),
        odbc_driver=os.getenv("ODBC_DRIVER") or None,

        # OCR
        tesseract_cmd=os.getenv("TESSERACT_CMD") or None,
        tesseract_lang=os.getenv("TESSERACT_LANG", "por+eng"),
        ocr_engine=os.getenv("OCR_ENGINE", "tesseract"),
        paddleocr_lang=os.getenv("PADDLEOCR_LANG", "pt"),
        ocr_table_whitelist=os.getenv(
            "OCR_TABLE_WHITELIST",
            "0123456789.,-LlTtWwCcDd aboréáíú",
        ),

        # Imagem
        default_resize_width=int(os.getenv("DEFAULT_RESIZE_WIDTH", "2200")),
        png_compress_level=int(os.getenv("PNG_COMPRESS_LEVEL", "3")),

        # ROI / Debug
        roi_strategy=os.getenv("ROI_STRATEGY", "contour"),
        save_debug_images=os.getenv("SAVE_DEBUG_IMAGES", "false").lower() in ("1", "true", "yes"),

        # Logging
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_file=Path(log_file_raw) if log_file_raw else None,

        # I/O
        default_output_dir=Path(os.getenv("DEFAULT_OUTPUT_DIR", "output")),
        dicom_roots=[
            r.strip()
            for r in os.getenv(
                "DICOM_ROOTS",
                r"\\192.168.1.155\dcms_new_ssd_01\DCMs"
                r";\\192.168.1.155\Dados\Database\Dcms"
                r";\\192.168.1.60\dcms",
            ).split(";")
            if r.strip()
        ],
    )

