#!/usr/bin/env python3

import ast
import csv
import json
import re
import sys
from pathlib import Path

import yaml


def load_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_yaml(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or default


def write_json(path, data):
    with Path(path).open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def parse_dict_from_line(line):
    if "{" not in line:
        return None
    try:
        text = line[line.index("{"):].strip()
        return ast.literal_eval(text)
    except Exception:
        return None


def parse_flower_log(log_text):
    metrics = {
        "flower_run_id": None,
        "actual_num_rounds": None,
        "last_round_seen": None,
        "strategy_duration_s": None,
        "initial_global": {},
        "rounds": {},
        "final_results_present": False,
        "train_failures_total": 0,
        "evaluate_failures_total": 0,
    }

    current_round = None
    next_metric_type = None
    expecting_global_metric = False

    for raw_line in log_text.splitlines():
        line = raw_line.strip()

        m = re.search(r"Successfully started run\s+([0-9]+)", line)
        if m:
            metrics["flower_run_id"] = m.group(1)

        m = re.search(r"Number of rounds:\s+([0-9]+)", line)
        if m:
            metrics["actual_num_rounds"] = int(m.group(1))

        m = re.search(r"\[ROUND\s+([0-9]+)/([0-9]+)\]", line)
        if m:
            current_round = int(m.group(1))
            metrics["last_round_seen"] = current_round
            metrics["rounds"].setdefault(str(current_round), {})
            continue

        m = re.search(r"Strategy execution finished in\s+([0-9.]+)s", line)
        if m:
            metrics["strategy_duration_s"] = float(m.group(1))

        if "Final results:" in line:
            metrics["final_results_present"] = True

        m = re.search(r"Initial global evaluation results:\s+(\{.*\})", line)
        if m:
            try:
                metrics["initial_global"] = ast.literal_eval(m.group(1))
            except Exception:
                pass

        if "aggregate_train:" in line and current_round is not None:
            m = re.search(r"Received\s+([0-9]+)\s+results and\s+([0-9]+)\s+failures", line)
            if m:
                metrics["rounds"].setdefault(str(current_round), {})
                metrics["rounds"][str(current_round)]["train_results"] = int(m.group(1))
                metrics["rounds"][str(current_round)]["train_failures"] = int(m.group(2))
                metrics["train_failures_total"] += int(m.group(2))
                next_metric_type = "train"
            continue

        if "aggregate_evaluate:" in line and current_round is not None:
            m = re.search(r"Received\s+([0-9]+)\s+results and\s+([0-9]+)\s+failures", line)
            if m:
                metrics["rounds"].setdefault(str(current_round), {})
                metrics["rounds"][str(current_round)]["evaluate_results"] = int(m.group(1))
                metrics["rounds"][str(current_round)]["evaluate_failures"] = int(m.group(2))
                metrics["evaluate_failures_total"] += int(m.group(2))
                next_metric_type = "evaluate"
            continue

        if "Global evaluation" in line and current_round is not None:
            expecting_global_metric = True
            continue

        if "Aggregated MetricRecord:" in line and current_round is not None:
            d = parse_dict_from_line(line)
            if d is None:
                continue

            metrics["rounds"].setdefault(str(current_round), {})

            if "train_loss" in d:
                metrics["rounds"][str(current_round)]["train_loss"] = float(d["train_loss"])
            elif "eval_loss" in d or "eval_acc" in d:
                if "eval_loss" in d:
                    metrics["rounds"][str(current_round)]["eval_loss"] = float(d["eval_loss"])
                if "eval_acc" in d:
                    metrics["rounds"][str(current_round)]["eval_acc"] = float(d["eval_acc"])

            next_metric_type = None
            continue

        if expecting_global_metric and "MetricRecord:" in line and current_round is not None:
            d = parse_dict_from_line(line)
            if d is not None:
                metrics["rounds"].setdefault(str(current_round), {})
                if "accuracy" in d:
                    metrics["rounds"][str(current_round)]["server_accuracy"] = float(d["accuracy"])
                if "loss" in d:
                    metrics["rounds"][str(current_round)]["server_loss"] = float(d["loss"])
            expecting_global_metric = False
            continue

    initial = metrics.get("initial_global") or {}
    if initial:
        metrics["rounds"].setdefault("0", {})
        if "accuracy" in initial:
            metrics["rounds"]["0"]["server_accuracy"] = float(initial["accuracy"])
        if "loss" in initial:
            metrics["rounds"]["0"]["server_loss"] = float(initial["loss"])

    return metrics


def write_round_csv(path, metrics):
    rounds = metrics.get("rounds", {})

    fieldnames = [
        "round",
        "train_loss",
        "eval_loss",
        "eval_acc",
        "server_accuracy",
        "server_loss",
        "train_results",
        "train_failures",
        "evaluate_results",
        "evaluate_failures",
    ]

    with Path(path).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for round_id in sorted(rounds.keys(), key=lambda x: int(x)):
            row = {"round": int(round_id)}
            row.update(rounds[round_id])
            writer.writerow(row)


def validate_run(run_dir, metrics):
    metadata = load_json(run_dir / "run_metadata.json", default={}) or {}
    config = load_yaml(run_dir / "experiment_config.yaml", default={}) or {}

    requested = (
        config
        .get("project", {})
        .get("run_config", {})
        .get("num-server-rounds")
    )

    return_code = metadata.get("return_code")

    checks = []

    def add_check(name, ok, expected=None, actual=None):
        checks.append({
            "name": name,
            "ok": bool(ok),
            "expected": expected,
            "actual": actual,
        })

    add_check("process_return_code", return_code == 0, 0, return_code)
    add_check("flower_run_id_present", bool(metrics.get("flower_run_id")), "present", metrics.get("flower_run_id"))
    add_check("final_results_present", bool(metrics.get("final_results_present")), True, metrics.get("final_results_present"))

    if requested is not None:
        add_check(
            "requested_rounds_match_actual_rounds",
            metrics.get("actual_num_rounds") == int(requested),
            int(requested),
            metrics.get("actual_num_rounds"),
        )
        add_check(
            "last_round_reached",
            metrics.get("last_round_seen") == int(requested),
            int(requested),
            metrics.get("last_round_seen"),
        )

    add_check("train_failures_zero", metrics.get("train_failures_total", 0) == 0, 0, metrics.get("train_failures_total", 0))
    add_check("evaluate_failures_zero", metrics.get("evaluate_failures_total", 0) == 0, 0, metrics.get("evaluate_failures_total", 0))

    valid = all(check["ok"] for check in checks)

    return {
        "valid": valid,
        "requested_num_server_rounds": requested,
        "actual_num_rounds": metrics.get("actual_num_rounds"),
        "last_round_seen": metrics.get("last_round_seen"),
        "checks": checks,
    }


def main():
    if len(sys.argv) != 2:
        print("Nutzung: python scripts/parse_flower_log.py runs/<RUN_ID>")
        sys.exit(1)

    run_dir = Path(sys.argv[1])
    log_path = run_dir / "flower.log"

    if not log_path.exists():
        print(f"Fehlt: {log_path}")
        sys.exit(1)

    log_text = log_path.read_text(encoding="utf-8", errors="replace")

    metrics = parse_flower_log(log_text)
    validation = validate_run(run_dir, metrics)

    write_json(run_dir / "flower_metrics.json", metrics)
    write_json(run_dir / "validation.json", validation)
    write_round_csv(run_dir / "metrics_rounds.csv", metrics)

    print(f"Flower Run ID: {metrics.get('flower_run_id')}")
    print(f"Requested rounds: {validation.get('requested_num_server_rounds')}")
    print(f"Actual rounds: {metrics.get('actual_num_rounds')}")
    print(f"Last round seen: {metrics.get('last_round_seen')}")
    print(f"Valid: {validation.get('valid')}")


if __name__ == "__main__":
    main()
