#!/usr/bin/env python3
from __future__ import annotations

import argparse, importlib.util, json, sys, traceback
from pathlib import Path

def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("ai_chat_legacy_v28", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--task-file", required=True)
    ap.add_argument("--raw-file", required=True)
    ap.add_argument("--state-file", required=True)
    ap.add_argument("--result-file", required=True)
    ns = ap.parse_args()

    legacy = load_module(Path.home() / ".local/share/orchbridge/app/ai_chat.py")
    repo = Path(ns.repo)
    task = Path(ns.task_file).read_text()
    raw = Path(ns.raw_file).read_text()
    state = json.loads(Path(ns.state_file).read_text())
    out = Path(ns.result_file)

    try:
        result = legacy.run_ai_orch(repo, task, raw, state)
        rc = int(result[0]) if len(result) > 0 else 1
        response = str(result[1]) if len(result) > 1 else ""
        stderr_text = str(result[2]) if len(result) > 2 else ""
        main_name = result[3] if len(result) > 3 else None
        task_status = result[4] if len(result) > 4 else None
        out.write_text(json.dumps({
            "rc": rc,
            "response": response,
            "stderr_text": stderr_text,
            "main_name": main_name,
            "task_status": task_status,
        }, ensure_ascii=False, indent=2) + "\n")
        return rc
    except KeyboardInterrupt:
        out.write_text(json.dumps({"rc": 130, "response": "", "task_status": None}, indent=2) + "\n")
        return 130
    except Exception as e:
        traceback.print_exc()
        out.write_text(json.dumps({
            "rc": 1, "response": "", "stderr_text": repr(e),
            "main_name": None, "task_status": None,
        }, ensure_ascii=False, indent=2) + "\n")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
