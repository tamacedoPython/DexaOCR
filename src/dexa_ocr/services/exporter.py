"""Exportação de resultados do pipeline DXA para JSON e arquivos auxiliares."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..models.dxa_models import DxaReport, MeasurementRow
from ..utils.logger import get_logger

log = get_logger("exporter")


def _find_row(report: DxaReport, site_type: str, region: str) -> Optional[MeasurementRow]:
    """Retorna a MeasurementRow de um sítio/região específicos, ou None."""
    for site in report.sites:
        if site.site_type == site_type:
            for row in site.rows:
                if row.region.lower() == region.lower():
                    return row
    return None


def _build_key_metrics(report: DxaReport) -> dict:
    """Extrai as métricas clínicas mais relevantes do relatório DXA.

    Prioridades clínicas:
    - Fêmur: Colo (neck) é o preditor primário de risco de fratura de quadril
    - Lombar: Total (L1-L4 ou linha 'Total') para avaliação global da coluna

    Resolve automaticamente nomes alternativos (ex.: 'Total' vs 'L1-L4'
    para a linha de soma lombar).
    """
    # ---- Lombar ----
    # Hologic usa "Total"; GE usa "L1-L4" como linha de agregação principal
    lumbar_total = (
        _find_row(report, "lumbar_spine", "Total")
        or _find_row(report, "lumbar_spine", "L1-L4")
    )

    # ---- Fêmur ----
    femur_site = next(
        (s for s in report.sites if "femur" in s.site_type),
        None,
    )
    femur_neck = _find_row(report, femur_site.site_type if femur_site else "", "Colo")
    femur_total = _find_row(report, femur_site.site_type if femur_site else "", "Total")
    femur_wards = _find_row(report, femur_site.site_type if femur_site else "", "Wards")
    femur_troc = (
        _find_row(report, femur_site.site_type if femur_site else "", "Troc.")
        or _find_row(report, femur_site.site_type if femur_site else "", "Troc")
    )

    def _bmd(row: Optional[MeasurementRow]) -> Optional[float]:
        return row.bmd if row else None

    def _t(row: Optional[MeasurementRow]) -> Optional[float]:
        return row.t_score if row else None

    def _z(row: Optional[MeasurementRow]) -> Optional[float]:
        return row.z_score if row else None

    def _ya(row: Optional[MeasurementRow]) -> Optional[int]:
        return row.young_adult_percent if row else None

    return {
        # Coluna lombar — agregado L1-L4 / Total
        "lumbar_bmd": _bmd(lumbar_total),
        "lumbar_t_score": _t(lumbar_total),
        "lumbar_z_score": _z(lumbar_total),
        "lumbar_young_adult_percent": _ya(lumbar_total),
        # Colo do fêmur — preditor primário de risco de fratura de quadril
        "femur_neck_bmd": _bmd(femur_neck),
        "femur_neck_t_score": _t(femur_neck),
        "femur_neck_z_score": _z(femur_neck),
        "femur_neck_young_adult_percent": _ya(femur_neck),
        # Total do fêmur proximal
        "femur_total_bmd": _bmd(femur_total),
        "femur_total_t_score": _t(femur_total),
        "femur_total_z_score": _z(femur_total),
        # Ward e Trocânter (secundários)
        "ward_bmd": _bmd(femur_wards),
        "ward_t_score": _t(femur_wards),
        "troc_bmd": _bmd(femur_troc),
        "troc_t_score": _t(femur_troc),
        # Lado do fêmur
        "femur_side": femur_site.site_type if femur_site else None,
    }


def save_outputs(
    report: DxaReport,
    output_dir: Path,
    raw_texts: dict[str, dict[str, str]],
) -> None:
    """Salva todos os artefatos de saída do pipeline.

    Parameters
    ----------
    report : DxaReport
        Relatório estruturado.
    output_dir : Path
        Diretório de saída.
    raw_texts : dict
        Mapeamento page_name → {roi_name: texto_ocr} com todos os textos brutos.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1) JSON estruturado principal
    result_path = output_dir / "result.json"
    result_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("JSON estruturado salvo em: %s", result_path)

    # 2) Texto bruto concatenado (para depuração)
    raw_path = output_dir / "ocr_raw.txt"
    raw_lines: list[str] = []
    for page_name, rois in raw_texts.items():
        raw_lines.append(f"=== {page_name} ===")
        for roi_name, text in rois.items():
            raw_lines.append(f"--- {roi_name} ---")
            raw_lines.append(text)
            raw_lines.append("")
    raw_path.write_text("\n".join(raw_lines), encoding="utf-8")
    log.info("Texto bruto salvo em: %s", raw_path)

    # 3) JSON de debug (OCR por ROI por página)
    debug_path = output_dir / "ocr_debug.json"
    debug_path.write_text(
        json.dumps(raw_texts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("JSON de debug salvo em: %s", debug_path)

    # 4) JSON estruturado com métricas clínicas-chave
    structured_path = output_dir / "ocr_structured.json"
    structured_path.write_text(
        json.dumps(_build_key_metrics(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("JSON de métricas-chave salvo em: %s", structured_path)
