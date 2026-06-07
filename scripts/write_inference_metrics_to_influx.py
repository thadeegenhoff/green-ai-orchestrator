#!/usr/bin/env python3

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS


NUMERIC_FIELDS = [
    "samples",
    "requests",
    "warmup_requests",
    "health_pings",
    "concurrency",
    "successful_requests",
    "failed_requests",
    "technical_success_rate",
    "top1_accuracy",
    "measured_seconds",
    "throughput_req_per_sec",
    "health_ping_ms_avg",
    "health_ping_ms_p50",
    "health_ping_ms_p95",
    "latency_ms_avg",
    "latency_ms_p50",
    "latency_ms_p90",
    "latency_ms_p95",
    "latency_ms_p99",
    "model_inference_ms_avg",
    "model_inference_ms_p50",
    "model_inference_ms_p95",
    "model_inference_ms_p99",
    "batch_size",
]


def load_env_file(path: Path):
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


def as_float(value, default=None):
    if value is None:
        return default

    try:
        return float(value)
    except Exception:
        return default


def clean_tag(value):
    if value is None:
        return "unknown"
    text = str(value).strip()
    return text if text else "unknown"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("summary_json", help="Pfad zu inference_summary.json")
    parser.add_argument("--env-file", default=".env.influx")

    parser.add_argument("--url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--org", default=None)
    parser.add_argument("--bucket", default=None)

    parser.add_argument("--train-run-dir", required=True)
    parser.add_argument("--eval-id", default=None)
    parser.add_argument("--mode", default="local_http")
    parser.add_argument("--model", default="cnn")
    parser.add_argument("--dataset", default="cifar10")
    parser.add_argument("--protocol", default="http")
    parser.add_argument("--inference-node", default="greenai-control")
    parser.add_argument("--benchmark-node", default="greenai-control")
    parser.add_argument("--batch-size", type=float, default=1.0)

    args = parser.parse_args()

    load_env_file(Path(args.env_file))

    url = args.url or os.environ.get("INFLUX_URL")
    token = args.token or os.environ.get("INFLUX_TOKEN")
    org = args.org or os.environ.get("INFLUX_ORG", "thade")
    bucket = args.bucket or os.environ.get("INFLUX_BUCKET", "greenaidata")

    if not url:
        raise SystemExit("Fehler: INFLUX_URL fehlt. Setze es in .env.influx oder per --url.")
    if not token:
        raise SystemExit("Fehler: INFLUX_TOKEN fehlt. Setze es in .env.influx oder per --token.")

    summary_path = Path(args.summary_json)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    train_run_dir = Path(args.train_run_dir)
    train_run_id = train_run_dir.name

    eval_id = args.eval_id or summary_path.parent.name

    concurrency_value = as_float(summary.get("concurrency"), 1.0)
    concurrency_tag = str(int(concurrency_value)) if concurrency_value is not None else "unknown"

    batch_size_value = float(args.batch_size)
    batch_size_tag = str(int(batch_size_value))

    point = (
        Point("fl_inference_summary")
        .tag("eval_id", clean_tag(eval_id))
        .tag("train_run_id", clean_tag(train_run_id))
        .tag("mode", clean_tag(args.mode))
        .tag("dataset", clean_tag(args.dataset))
        .tag("model", clean_tag(args.model))
        .tag("protocol", clean_tag(args.protocol))
        .tag("inference_node", clean_tag(args.inference_node))
        .tag("benchmark_node", clean_tag(args.benchmark_node))
        .tag("concurrency", clean_tag(concurrency_tag))
        .tag("batch_size", clean_tag(batch_size_tag))
        .time(datetime.now(timezone.utc), WritePrecision.NS)
    )

    summary["batch_size"] = batch_size_value

    written_fields = 0

    for field in NUMERIC_FIELDS:
        value = as_float(summary.get(field))
        if value is None:
            continue

        # Wichtig: alles als Float schreiben, damit Flux/Grafana keine int/float-Schema-Kollision bekommt.
        point = point.field(field, float(value))
        written_fields += 1

    point = point.field("valid", 1.0)

    with InfluxDBClient(url=url, token=token, org=org) as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=bucket, org=org, record=point)

    print("Inference-Metriken nach InfluxDB geschrieben.")
    print(f"Measurement: fl_inference_summary")
    print(f"Bucket: {bucket}")
    print(f"Org: {org}")
    print(f"Eval ID: {eval_id}")
    print(f"Train Run ID: {train_run_id}")
    print(f"Fields geschrieben: {written_fields + 1}")


if __name__ == "__main__":
    main()
