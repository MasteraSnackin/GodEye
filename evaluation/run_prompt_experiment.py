import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "evaluation" / "replay_eval_dataset.json"
OUT_DIR = ROOT / "evaluation" / "results"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def wait_for_health(base_url: str, timeout_s: int = 45) -> None:
    deadline = time.time() + timeout_s
    last_err = None
    while time.time() < deadline:
        try:
            resp = requests.get(f"{base_url}/health", timeout=4)
            if resp.ok:
                return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(0.5)
    raise RuntimeError(f"API did not become healthy: {last_err}")


def score_response(payload: dict) -> float:
    narrative = str(payload.get("narrative", "") or "").lower()
    summary = str(payload.get("event_summary", "") or "").lower()
    events = payload.get("events") or []
    status_n = payload.get("narrative_status", "error")
    status_s = payload.get("summary_status", "error")
    runtime = payload.get("runtime_metrics") or {}
    total_ms = float(runtime.get("total_ms") or 0.0)

    score = 0.0
    if status_n == "ok":
        score += 2.0
    if status_s == "ok":
        score += 1.0
    if len(events) > 0:
        score += 1.0
    keywords = ["correlation", "jamming", "confidence", "evidence"]
    hits = sum(1 for k in keywords if k in narrative or k in summary)
    score += hits * 0.35
    if total_ms > 0:
        score += max(0.0, 1.0 - (total_ms / 12000.0))
    return round(score, 4)


def run_variant(variant: str, port: int, dataset: list[dict]) -> dict:
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["GODEYE_PROMPT_VERSION"] = variant
    env.setdefault("LANGCHAIN_TRACING_V2", "true")
    env.setdefault("LANGCHAIN_PROJECT", "GodEye")

    proc = subprocess.Popen(  # noqa: S603
        ["python", "-m", "uvicorn", "api.replay.api:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_health(base_url)
        rows = []
        for case in dataset:
            started = time.time()
            resp = requests.post(f"{base_url}/api/replay", json=case, timeout=180)
            resp.raise_for_status()
            payload = resp.json()
            elapsed_ms = round((time.time() - started) * 1000, 2)
            row = {
                "case_id": case["id"],
                "query": case["query"],
                "from_time": case["from_time"],
                "to_time": case["to_time"],
                "event_count": len(payload.get("events") or []),
                "narrative_status": payload.get("narrative_status"),
                "summary_status": payload.get("summary_status"),
                "runtime_ms": (payload.get("runtime_metrics") or {}).get("total_ms"),
                "http_elapsed_ms": elapsed_ms,
                "llm_model_used": payload.get("llm_model_used"),
                "trace_url": payload.get("trace_url") or "",
                "thread_id": payload.get("thread_id"),
                "score": score_response(payload),
            }
            rows.append(row)

        avg_score = round(sum(r["score"] for r in rows) / max(1, len(rows)), 4)
        avg_latency = round(sum(float(r.get("runtime_ms") or 0.0) for r in rows) / max(1, len(rows)), 2)
        result = {
            "variant": variant,
            "port": port,
            "avg_score": avg_score,
            "avg_runtime_ms": avg_latency,
            "cases": rows,
        }
        out_file = OUT_DIR / f"experiment_{variant}.json"
        out_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def choose_winner(v1: dict, v2: dict) -> dict:
    if v1["avg_score"] > v2["avg_score"]:
        return v1
    if v2["avg_score"] > v1["avg_score"]:
        return v2
    return v1 if v1["avg_runtime_ms"] <= v2["avg_runtime_ms"] else v2


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(DATASET_PATH))
    parser.add_argument("--v1", default="v1")
    parser.add_argument("--v2", default="v2")
    parser.add_argument("--port1", type=int, default=8011)
    parser.add_argument("--port2", type=int, default=8012)
    args = parser.parse_args()

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    r1 = run_variant(args.v1, args.port1, dataset)
    r2 = run_variant(args.v2, args.port2, dataset)
    winner = choose_winner(r1, r2)
    summary = {"winner": winner["variant"], "results": [r1, r2]}
    (OUT_DIR / "experiment_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
