from __future__ import annotations

import logging
import os
from pathlib import Path

import pyodbc

log = logging.getLogger("dicom_locator")


def get_remote_dicom_root(conn: pyodbc.Connection) -> str:
    query = "SELECT TOP 1 [DicomPath] FROM [AgileAI].[dbo].[Tbl_IA_Configuracoes]"
    cur = conn.cursor()
    cur.execute(query)
    row = cur.fetchone()
    if not row:
        raise ValueError("Campo DicomPath nao encontrado em Tbl_IA_Configuracoes.")
    return str(row.DicomPath).rstrip("\\/")


def _extract_suffix_after_dcms(filename: str) -> str:
    """Extrai a parte do caminho ap?s a pasta 'DCMs' (case-insensitive)."""
    normalized = os.path.normpath(filename)
    parts = normalized.split(os.sep)
    upper_parts = [p.upper() for p in parts]
    if "DCMS" not in upper_parts:
        raise ValueError(f"Pasta 'DCMs' n?o encontrada em: {filename}")
    idx = upper_parts.index("DCMS")
    return os.path.join(*parts[idx + 1:]) if idx + 1 < len(parts) else ""


def resolve_dicom_path(filename: str, dicom_roots: list[str]) -> Path | None:
    """Tenta cada root em ordem, retorna o primeiro que cont?m o arquivo.

    Parameters
    ----------
    filename:
        Caminho armazenado no banco de dados (pode ser de qualquer servidor).
    dicom_roots:
        Lista de paths raiz a tentar, em ordem de prefer?ncia.
    """
    try:
        suffix = _extract_suffix_after_dcms(filename)
    except ValueError as exc:
        log.warning("resolve_dicom_path: %s", exc)
        return None

    for root in dicom_roots:
        root_norm = os.path.normpath(root)
        # Se o root j? termina em 'DCMs', n?o duplicar
        if root_norm.upper().endswith("DCMS"):
            candidate = Path(os.path.join(root_norm, suffix))
        else:
            candidate = Path(os.path.join(root_norm, "DCMs", suffix))
        if candidate.exists():
            log.debug("DICOM encontrado em root '%s': %s", root, candidate)
            return candidate
        log.debug("DICOM n?o encontrado em root '%s': %s", root, candidate)

    log.warning("DICOM n?o encontrado em nenhum root: %s", filename)
    return None


def get_dicom_paths_by_study_uid(
    conn: pyodbc.Connection,
    study_uid: str,
    dicom_roots: list[str] | None = None,
) -> list[Path]:
    """Busca os caminhos dos DICOMs de um estudo, tentando os roots em ordem.

    Parameters
    ----------
    conn:
        Conex?o SQL Server aberta.
    study_uid:
        StudyUID do exame.
    dicom_roots:
        Lista de roots a tentar. Se None, consulta o banco via
        ``get_remote_dicom_root`` (comportamento original).
    """
    if dicom_roots is None:
        root = get_remote_dicom_root(conn)
        dicom_roots = [root]

    query = """
    SELECT i.filename
    FROM DicomServerDB.dbo.ImageTable i
    JOIN DicomServerDB.dbo.SeriesTable s ON i.SeriesUID_FKey = s.SeriesUID
    WHERE s.StudyUID_FKey = ?
    ORDER BY i.filename
    """
    cur = conn.cursor()
    cur.execute(query, study_uid)

    output: list[Path] = []
    for row in cur.fetchall():
        resolved = resolve_dicom_path(str(row.filename), dicom_roots)
        if resolved is not None:
            output.append(resolved)
    return output
