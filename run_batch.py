"""Batch test: processa os StudyUIDs do piloto e imprime tabela-resumo de extração.

Uso:
    python run_batch.py [--engine paddleocr|tesseract] [--debug] [--workers N]

Cada estudo é processado sequencialmente (PaddleOCR já usa GPU/paralelismo interno).
O resultado é salvo em output/batch/<uid_truncado>/result.json.
Ao final, é exibida uma tabela com os campos capturados por estudo.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Lista de StudyUIDs do piloto (fornecidos pelo usuário)
# ---------------------------------------------------------------------------

STUDY_UIDS = [
    "1.2.840.113850.198164254087037192235129222199167004009054007223",
    "1.2.840.113850.239159148044197160084191035114235120021209104110",
    "1.2.840.113850.227111221030230105014132236251108016024074218107",
    "1.2.840.113850.224221211233196244151124179012051109229180180024",
    "1.2.840.113619.2.110.152033.20260513092614",
    "1.2.840.113619.2.110.152033.20260513082249",
    "1.2.840.113619.2.110.152033.20260513100543",
    "1.2.840.113619.2.110.152033.20260513074644",
    "1.2.840.113619.2.110.152033.20260513112817",
    "1.2.840.113619.2.110.152033.20260513080815",
    "1.2.840.113619.2.110.152033.20260513095329",
    "1.2.840.113619.2.110.152033.20260513104724",
    "1.2.840.113619.2.110.152033.20260513083716",
    "1.2.840.113619.2.110.152033.20260513133853",
    "1.2.840.113619.2.110.152033.20260513091035",
    "1.2.840.113619.2.110.152033.20260513135453",
    "1.2.840.113619.2.110.152033.20260513094110",
    "1.2.840.113619.2.110.152033.20260513132338",
    "1.2.840.113619.2.110.152033.20260506101345",
    "1.2.840.113619.2.110.152033.20260507112405",
    "1.2.840.113619.2.110.152033.20260514084342",
]

# Campos a monitorar no ocr_structured.json
TRACKED_FIELDS = [
    "lumbar_bmd",
    "lumbar_t_score",
    "femur_neck_bmd",
    "femur_neck_t_score",
    "femur_neck_z_score",
    "femur_total_bmd",
    "femur_total_t_score",
]

# Campos adicionais monitorados no result.json (L1-L4 individuais)
LUMBAR_LEVELS = ["L1", "L2", "L3", "L4"]

BATCH_OUTPUT_ROOT = Path("output") / "batch"


def uid_label(uid: str) -> str:
    """Retorna rótulo curto para exibição (últimos 20 chars)."""
    return f"...{uid[-20:]}"


def run_study(uid: str, output_dir: Path, engine: str | None, debug: bool) -> dict | None:
    """Executa o pipeline para um StudyUID. Retorna o result.json ou None."""
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "main.py",
        "--study-uid", uid,
        "--output-dir", str(output_dir),
    ]
    if engine:
        cmd += ["--engine", engine]
    if debug:
        cmd += ["--debug"]

    print(f"\n{'='*60}")
    print(f"UID: {uid_label(uid)}")
    print(f"Saída: {output_dir}")
    print(f"Comando: {' '.join(cmd)}")
    print(f"{'='*60}")

    proc = subprocess.run(cmd, capture_output=False, text=True)

    result_file = output_dir / "result.json"
    if result_file.exists():
        with open(result_file, encoding="utf-8") as f:
            return json.load(f)
    else:
        print(f"[ERRO] result.json não gerado para {uid_label(uid)} (exit={proc.returncode})")
        return None


def check_field(d: dict, *keys: str) -> bool:
    """Retorna True se o caminho de chaves levar a um valor não-None."""
    cur: object = d
    for k in keys:
        if not isinstance(cur, dict):
            return False
        cur = cur.get(k)
    return cur is not None


def build_summary(results: dict[str, dict | None]) -> str:
    """Constrói a tabela-resumo de extração."""
    col_w = 22

    # Colunas: Label | structured fields... | L1..L4
    header_cols = ["UID"] + TRACKED_FIELDS + LUMBAR_LEVELS
    header = " | ".join(c[:col_w].ljust(col_w) for c in header_cols)
    sep = "-+-".join("-" * col_w for _ in header_cols)

    lines = [header, sep]

    for uid, result in results.items():
        label = uid_label(uid)[:col_w].ljust(col_w)
        if result is None:
            row_cols = [label] + ["FALHA" + " " * (col_w - 5)] * (len(header_cols) - 1)
        else:
            structured_path = BATCH_OUTPUT_ROOT / uid / "ocr_structured.json"
            structured = {}
            if structured_path.exists():
                with open(structured_path, encoding="utf-8") as f:
                    structured = json.load(f)

            row_cols = [label]
            # structured fields
            for field in TRACKED_FIELDS:
                val = structured.get(field)
                cell = str(val) if val is not None else "null"
                row_cols.append(cell[:col_w].ljust(col_w))

            # L1-L4 individual from result.json
            lumbar = result.get("lumbar_spine", {})
            for level in LUMBAR_LEVELS:
                lv = lumbar.get(level, {})
                bmd = lv.get("bmd") if lv else None
                cell = str(bmd) if bmd is not None else "null"
                row_cols.append(cell[:col_w].ljust(col_w))

        lines.append(" | ".join(row_cols))

    # Contagem de campos preenchidos
    total_studies = len(results)
    successful = sum(1 for v in results.values() if v is not None)
    lines.append("")
    lines.append(f"Estudos processados: {total_studies} | Sucesso: {successful} | Falhas: {total_studies - successful}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch DexaOCR — processa lista de StudyUIDs")
    parser.add_argument("--engine", choices=["tesseract", "paddleocr"], help="Engine OCR")
    parser.add_argument("--debug", action="store_true", help="Salvar imagens de debug")
    parser.add_argument(
        "--uids",
        nargs="*",
        help="Subconjunto de UIDs (por índice 0-based, ex.: --uids 0 1 2). Padrão: todos",
    )
    args = parser.parse_args()

    uids_to_run = STUDY_UIDS
    if args.uids:
        indices = [int(i) for i in args.uids]
        uids_to_run = [STUDY_UIDS[i] for i in indices]

    print(f"Batch DexaOCR — {len(uids_to_run)} estudos a processar")
    print(f"Engine: {args.engine or 'padrão (.env)'}")
    print(f"Debug: {args.debug}")

    results: dict[str, dict | None] = {}
    for uid in uids_to_run:
        out_dir = BATCH_OUTPUT_ROOT / uid
        result = run_study(uid, out_dir, args.engine, args.debug)
        results[uid] = result

    summary = build_summary(results)
    print("\n\n" + "=" * 80)
    print("RESUMO DE EXTRAÇÃO")
    print("=" * 80)
    print(summary)

    # Salvar resumo em arquivo
    summary_path = BATCH_OUTPUT_ROOT / "batch_summary.txt"
    BATCH_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary, encoding="utf-8")
    print(f"\nResumo salvo em: {summary_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
