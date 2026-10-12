"""Compare a recorded session, retaining source events and raw data unchanged."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ninefbg.comparison import compare_algorithms, training_gate
from ninefbg.storage import clean
from ninefbg.tracking import ALGORITHM_VERSION


def main():
    parser = argparse.ArgumentParser(description="用会话的同一批原始光谱比较两种算法")
    parser.add_argument("session", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    source = args.session / "raw_spectra.jsonl" if args.session.is_dir() else args.session
    frames = [json.loads(line) for line in source.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    config = json.loads(args.config.read_text(encoding="utf-8-sig")) if args.config else None
    report = compare_algorithms(frames, config)
    report["source_file"] = str(source.resolve())
    report["provisional_training_gate"] = training_gate(report, ALGORITHM_VERSION)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(clean(report), stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"原始帧数":report["original_frame_count"], "比较帧数":report["compared_frame_count"],
                      "报告":str(args.output.resolve()),"训练限制":report["provisional_training_gate"]["reasons"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
