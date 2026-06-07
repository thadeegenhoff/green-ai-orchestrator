#!/usr/bin/env python3

import ast
import csv
import re
import sys
from pathlib import Path


SERVER_METRIC_MAP = {
    "accuracy": "server_accuracy",
    "loss": "server_loss",
    "precision_macro": "server_precision_macro",
    "recall_macro": "server_recall_macro",
    "f1_macro": "server_f1_macro",
    "precision_weighted": "server_precision_weighted",
    "recall_weighted": "server_recall_weighted",
    "f1_weighted": "server_f1_weighted",
}


def parse_metric_dict(text):
    return ast.literal_eval(text.strip())


def normalize_server_metrics(metrics):
    normalized = {}

    for key, value in metrics.items():
        out_key = SERVER_METRIC_MAP.get(key, key)

        if not (
            out_key.startswith("server_")
            or out_key in ["server_accuracy", "server_loss"]
        ):
            continue

        try:
            normalized[out_key] = str(float(value))
        except Exception:
            continue

    return normalized


def read_existing_csv(csv_path):
    if not csv_path.exists():
        return [], []

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    return fieldnames, rows


def write_csv(csv_path, fieldnames, rows):
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main():
    if len(sys.argv) != 2:
        print("Nutzung: python scripts/augment_round_metrics_from_flower_log.py runs/<RUN_ID>")
        sys.exit(1)

    run_dir = Path(sys.argv[1])
    log_path = run_dir / "flower.log"
    csv_path = run_dir / "metrics_rounds.csv"

    if not log_path.exists():
        raise SystemExit(f"Fehlt: {log_path}")

    fieldnames, rows = read_existing_csv(csv_path)

    rows_by_round = {}
    for row in rows:
        rows_by_round[str(row.get("round", ""))] = row

    current_round = None
    expect_global_metric = False
    parsed_by_round = {}

    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        round_match = re.search(r"\[ROUND\s+(\d+)/", line)
        if round_match:
            current_round = round_match.group(1)

        if "Initial global evaluation results:" in line:
            payload = line.split("Initial global evaluation results:", 1)[1].strip()
            metrics = normalize_server_metrics(parse_metric_dict(payload))
            parsed_by_round.setdefault("0", {}).update(metrics)
            continue

        if "Global evaluation" in line:
            expect_global_metric = True
            continue

        if expect_global_metric and "MetricRecord:" in line and "Aggregated MetricRecord" not in line:
            payload = line.split("MetricRecord:", 1)[1].strip()
            metrics = normalize_server_metrics(parse_metric_dict(payload))

            if current_round is not None:
                parsed_by_round.setdefault(str(current_round), {}).update(metrics)

            expect_global_metric = False
            continue

    if not parsed_by_round:
        raise SystemExit("Keine Server-Metriken im Flower-Log gefunden.")

    if not fieldnames:
        fieldnames = ["round"]

    for round_id, metrics in parsed_by_round.items():
        row = rows_by_round.get(round_id)
        if row is None:
            row = {"round": round_id}
            rows_by_round[round_id] = row

        for key, value in metrics.items():
            row[key] = value
            if key not in fieldnames:
                fieldnames.append(key)

    def round_sort_key(item):
        key, _ = item
        try:
            return int(key)
        except Exception:
            return 999999

    rows = [row for _, row in sorted(rows_by_round.items(), key=round_sort_key)]

    preferred_order = [
        "round",
        "train_loss",
        "eval_loss",
        "eval_acc",
        "server_accuracy",
        "server_loss",
        "server_precision_macro",
        "server_recall_macro",
        "server_f1_macro",
        "server_precision_weighted",
        "server_recall_weighted",
        "server_f1_weighted",
    ]

    ordered = []
    for col in preferred_order:
        if col in fieldnames and col not in ordered:
            ordered.append(col)

    for col in fieldnames:
        if col not in ordered:
            ordered.append(col)

    for row in rows:
        for col in ordered:
            row.setdefault(col, "")

    write_csv(csv_path, ordered, rows)

    print(f"CSV erweitert: {csv_path}")
    print("Ergänzte Runden:")
    for round_id, metrics in parsed_by_round.items():
        print(f"  round={round_id}: {', '.join(sorted(metrics.keys()))}")


if __name__ == "__main__":
    main()
