#!/usr/bin/env python3
"""Run an isolated app build and request one bounded AI-guided recovery attempt."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.github_actions_client import call_apphoryn_internal_api


RECOVERY_ACTIONS = {
    "android": {"retry_same", "gradle_refresh_dependencies", "none"},
    "windows": {"retry_same", "npm_reinstall", "dotnet_restore", "go_mod_download", "none"},
    "linux": {"retry_same", "npm_reinstall", "dotnet_restore", "go_mod_download", "none"},
    "macos": {"retry_same", "npm_reinstall", "dotnet_restore", "go_mod_download", "none"},
}


def _redact(text: str) -> str:
    result = text
    for key, value in os.environ.items():
        secret_name = any(token in key.upper() for token in ("TOKEN", "PASSWORD", "SECRET", "PRIVATE_KEY"))
        if secret_name and value and len(value) >= 6:
            result = result.replace(value, "[REDACTED]")
    return result


def _run_build(repository: str, git_ref: str, target: str, output: Path, recovery_action: str) -> tuple[int, str]:
    env = os.environ.copy()
    env.pop("APPHORYN_BUILD_RECOVERY_ACTION", None)
    if recovery_action:
        env["APPHORYN_BUILD_RECOVERY_ACTION"] = recovery_action
    command = [
        sys.executable,
        str(ROOT / "scripts" / "ci_build.py"),
        "--repository", repository,
        "--ref", git_ref,
        "--target", target,
        "--output", str(output),
    ]
    print(
        json.dumps({"event": "build_attempt_started", "target": target,
                    "recovery_action": recovery_action or "initial"}, ensure_ascii=False),
        flush=True,
    )
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    captured: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        safe_line = _redact(line)
        captured.append(safe_line)
        print(safe_line, end="", flush=True)
    return_code = process.wait()
    return return_code, "".join(captured)[-12000:]


def _request_recovery(repository: str, git_ref: str, target: str, attempt: int, log: str) -> dict[str, Any]:
    return call_apphoryn_internal_api(
        "/api/v1/internal/github-actions/build-recovery",
        method="POST",
        payload={
            "repository_url": repository,
            "git_ref": git_ref,
            "target": target,
            "attempt": attempt,
            "error_log": _redact(log)[-12000:],
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one platform target with bounded AI-guided recovery.")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--target", required=True, choices=sorted(RECOVERY_ACTIONS))
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-retries", type=int, default=1, choices=(0, 1, 2))
    parser.add_argument("--report", default="build-recovery-report.json")
    args = parser.parse_args()

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "repository_url": args.repository,
        "git_ref": args.ref,
        "target": args.target,
        "attempts": [],
        "recovered": False,
        "final_status": "running",
    }

    code, log = _run_build(args.repository, args.ref, args.target, output, "")
    report["attempts"].append({"number": 1, "action": "initial", "exit_code": code})
    if code == 0:
        report["final_status"] = "success"
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0

    if args.max_retries <= 0:
        report["final_status"] = "failed_no_retry_configured"
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return code

    try:
        diagnosis = _request_recovery(args.repository, args.ref, args.target, 1, log)
    except Exception as exc:
        report["diagnosis_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        report["final_status"] = "failed_ai_diagnosis_unavailable"
        print(json.dumps({"event": "build_recovery_unavailable", "reason": report["diagnosis_error"]}, ensure_ascii=False), flush=True)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return code

    action = str(diagnosis.get("retry_action") or "none").strip().lower()
    if action not in RECOVERY_ACTIONS[args.target]:
        action = "none"
    report["ai_diagnosis"] = {
        "agent": diagnosis.get("debugging_agent"),
        "root_cause": str(diagnosis.get("root_cause") or "")[:500],
        "explanation": str(diagnosis.get("explanation") or "")[:1200],
        "confidence": diagnosis.get("confidence"),
        "provider": diagnosis.get("provider"),
        "model": diagnosis.get("model"),
        "retry_action": action,
    }
    print(json.dumps({
        "event": "build_recovery_diagnosis",
        "target": args.target,
        "root_cause": report["ai_diagnosis"]["root_cause"],
        "retry_action": action,
        "confidence": report["ai_diagnosis"]["confidence"],
    }, ensure_ascii=False), flush=True)

    if action == "none":
        report["final_status"] = "failed_no_safe_recovery"
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("No safe automatic recovery was identified. The Scout can continue with the next application.", flush=True)
        return code

    # Never let a failed first attempt leak partial files into the trusted publisher.
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    retry_code, retry_log = _run_build(args.repository, args.ref, args.target, output, action)
    report["attempts"].append({"number": 2, "action": action, "exit_code": retry_code})
    report["recovered"] = retry_code == 0
    report["final_status"] = "recovered" if retry_code == 0 else "failed_after_ai_recovery"
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if retry_code != 0:
        print("AI-guided recovery did not resolve this target. The Scout can continue with the next application.", flush=True)
    return retry_code


if __name__ == "__main__":
    raise SystemExit(main())
