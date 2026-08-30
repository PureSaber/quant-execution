from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "benchmark_replay.py"


def test_matching_worker_contract_uses_explicit_five_percent_fill_density(tmp_path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(BENCHMARK),
            "--worker",
            "matching_exact_ledger",
            "--events",
            "40",
            "--artifact-mode",
            "arrow",
            "--artifact-root",
            str(tmp_path),
            "--artifact-retention",
            "keep",
            "--artifact-batch-size",
            "8",
            "--artifact-queue-batches",
            "1",
            "--order-stride",
            "20",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["events"] == 40
    assert result["orders"] == result["fills"] == 2
    assert result["order_events"] == 4
    assert result["transactions"] == 5
    assert result["fill_density"] == 0.05
    assert result["order_stride"] == 20
    assert result["artifact_mode"] == "arrow"
    assert result["artifact_retention"] == "keep"
    assert result["artifact_cleanup"] == "none"
    assert result["artifact_files_removed"] == 0
    assert result["strict_verification_passed"] is True
    assert result["dependencies"]["quant_data_kit"] == "0.7.4"
    assert Path(result["artifact_path"]).is_dir()
    assert result["artifact_manifest_sha256"]
    assert result["artifact_file_sha256"]
