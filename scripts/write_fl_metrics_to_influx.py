#!/usr/bin/env python3

import csv
import json
import os
import sys
from pathlib import Path
from datetime import timezone
from dateutil.parser import isoparse

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS


INFLUX_URL = os.environ["INFLUX_URL"]
INFLUX_ORG = os.environ["INFLUX_ORG"]
INFLUX_BUCKET = os.environ["INFLUX_BUCKET"]
INFLUX_TOKEN = os.environ["INFLUX_TOKEN"]


def load_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_csv_rows(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def parse_time(value):
    if not value:
        return None
    dt = isoparse(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def to_int(value):
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def add_common_tags(point, meta, summary):
    experiment = meta.get("experiment", {}) or {}

    return (
        point
        .tag("run_id", meta.get("run_id", "unknown"))
        .tag("k8s_run_id", meta.get("k8s_run_id", "unknown"))
        .tag("flower_run_id", summary.get("flower_run_id") or "unknown")
        .tag("experiment_name", experiment.get("name", "unknown"))
        .tag("strategy", summary.get("strategy") or meta.get("strategy", "unknown"))
        .tag("dataset", summary.get("dataset") or meta.get("dataset", "unknown"))
        .tag("split", summary.get("split") or experiment.get("split", "unknown"))
        .tag("model", summary.get("model") or meta.get("model", "unknown"))
    )


def main():
    if len(sys.argv) != 2:
        print("Nutzung: python scripts/write_fl_metrics_to_influx.py runs/<RUN_ID>")
        sys.exit(1)

    run_dir = Path(sys.argv[1])

    meta = load_json(run_dir / "run_metadata.json", default={}) or {}
    summary = load_json(run_dir / "summary.json", default={}) or {}
    metrics = load_json(run_dir / "flower_metrics.json", default={}) or {}
    validation = load_json(run_dir / "validation.json", default={}) or {}
    round_rows = read_csv_rows(run_dir / "metrics_rounds.csv")

    end_time = parse_time(meta.get("end_time")) or parse_time(meta.get("start_time"))
    if end_time is None:
        print("Fehler: Keine gültige end_time/start_time in run_metadata.json")
        sys.exit(1)

    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)

        # 1) Runde-für-Runde-Metriken
        for row in round_rows:
            round_no = to_int(row.get("round"))
            if round_no is None:
                continue

            p = Point("fl_round_metrics")
            p = add_common_tags(p, meta, summary)
            p = p.tag("round", str(round_no))
            p = p.field("round", round_no)

            for field in [
                "train_loss",
                "eval_loss",
                "eval_acc",
                "server_accuracy",
                "server_loss",
            ]:
                value = to_float(row.get(field))
                if value is not None:
                    p = p.field(field, value)

            for field in [
                "train_results",
                "train_failures",
                "evaluate_results",
                "evaluate_failures",
            ]:
                value = to_int(row.get(field))
                if value is not None:
                    p = p.field(field, value)

            p = p.time(end_time, WritePrecision.NS)
            write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=p)

        # 2) Experiment-Zusammenfassung
        p = Point("fl_experiment_summary")
        p = add_common_tags(p, meta, summary)

        numeric_summary_fields = [
            "requested_num_server_rounds",
            "actual_num_rounds",
            "last_round_seen",
            "local_epochs",
            "batch_size",
            "learning_rate",
            "strategy_duration_s",
            "final_accuracy",
            "final_loss",
            "total_energy_wh",
            "train_failures_total",
            "evaluate_failures_total",
        ]

        for field in numeric_summary_fields:
            value = to_float(summary.get(field))
            if value is not None:
                p = p.field(field, value)

        p = (
            p.field("valid", bool(summary.get("valid", validation.get("valid", False))))
             .field("status", str(summary.get("status", meta.get("status", "unknown"))))
             .time(end_time, WritePrecision.NS)
        )

        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=p)

        # 3) Validation
        p = Point("fl_validation")
        p = add_common_tags(p, meta, summary)

        p = (
            p.field("valid", bool(validation.get("valid", False)))
             .field("requested_num_server_rounds", int(validation.get("requested_num_server_rounds") or 0))
             .field("actual_num_rounds", int(validation.get("actual_num_rounds") or 0))
             .field("last_round_seen", int(validation.get("last_round_seen") or 0))
             .field("train_failures_total", int(metrics.get("train_failures_total") or 0))
             .field("evaluate_failures_total", int(metrics.get("evaluate_failures_total") or 0))
             .field("final_results_present", bool(metrics.get("final_results_present", False)))
             .time(end_time, WritePrecision.NS)
        )

        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=p)

    print(f"FL-Metriken nach InfluxDB exportiert: {run_dir}")
    print("Measurements: fl_round_metrics, fl_experiment_summary, fl_validation")


if __name__ == "__main__":
    main()
