"""Fail-closed ALR contract and timing comparison for two source checkouts."""

from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time


VOLATILE_METADATA = {
    "cache_hit",
    "created_at",
    "elapsed_seconds",
    "legalpdf_parser_version",
}


def _stable(value):
    if isinstance(value, dict):
        return {
            key: _stable(item)
            for key, item in value.items()
            if key not in VOLATILE_METADATA
        }
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def _worker(repo: Path, pdf: Path) -> int:
    sys.path.insert(0, str(repo))
    import alr_quote_verifier as app

    started = time.perf_counter()
    with contextlib.redirect_stdout(sys.stderr):
        parsed = app._load_parsed_document(pdf)
    elapsed = time.perf_counter() - started
    print(
        json.dumps(
            {
                "contract": _stable(
                    {
                        "paragraphs": parsed.paragraphs,
                        "footnotes": parsed.footnotes,
                        "author_links": parsed.author_links,
                        "footnote_order": parsed.footnote_order,
                        "metadata": parsed.metadata,
                    }
                ),
                "elapsed_seconds": elapsed,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _load(script: Path, repo: Path, pdf: Path) -> dict:
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(script),
            "--worker",
            "--repo",
            str(repo),
            "--pdf",
            str(pdf),
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def _speedup(baseline: float, candidate: float) -> float:
    return round((baseline - candidate) / baseline * 100, 2) if baseline else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--pdf", type=Path, action="append", required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--require-faster", action="store_true")
    args = parser.parse_args()
    if args.worker:
        if args.repo is None or len(args.pdf) != 1:
            parser.error("worker mode requires one --repo and one --pdf")
        return _worker(args.repo.resolve(), args.pdf[0].resolve())
    if args.baseline is None or args.candidate is None or args.repeat < 1:
        parser.error("comparison requires both repos and a positive --repeat")

    script = Path(__file__).resolve()
    timings = {"baseline": [], "candidate": []}
    per_pdf = {}
    differences = []
    for pdf in args.pdf:
        pdf_timings = {"baseline": [], "candidate": []}
        baseline_contract = None
        candidate_contract = None
        for iteration in range(args.repeat):
            order = (
                (("baseline", args.baseline), ("candidate", args.candidate))
                if iteration % 2 == 0
                else (("candidate", args.candidate), ("baseline", args.baseline))
            )
            for label, repo in order:
                result = _load(script, repo.resolve(), pdf.resolve())
                timings[label].append(result["elapsed_seconds"])
                pdf_timings[label].append(result["elapsed_seconds"])
                if label == "baseline":
                    baseline_contract = result["contract"]
                else:
                    candidate_contract = result["contract"]
        if baseline_contract != candidate_contract:
            differences.append(str(pdf))
        pdf_medians = {
            key: statistics.median(values) for key, values in pdf_timings.items()
        }
        per_pdf[str(pdf.resolve())] = {
            "median_seconds": pdf_medians,
            "candidate_speedup_percent": _speedup(
                pdf_medians["baseline"], pdf_medians["candidate"]
            ),
            "candidate_faster": pdf_medians["candidate"] < pdf_medians["baseline"],
        }

    medians = {key: statistics.median(values) for key, values in timings.items()}
    slower_pdfs = [
        path for path, result in per_pdf.items() if not result["candidate_faster"]
    ]
    report = {
        "pdf_count": len(args.pdf),
        "repeat": args.repeat,
        "contract_differences": differences,
        "median_seconds": medians,
        "candidate_speedup_percent": _speedup(
            medians["baseline"], medians["candidate"]
        ),
        "per_pdf": per_pdf,
        "not_faster": slower_pdfs,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return int(bool(differences) or (args.require_faster and bool(slower_pdfs)))


if __name__ == "__main__":
    raise SystemExit(main())
