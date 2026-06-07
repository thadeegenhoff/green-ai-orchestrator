#!/usr/bin/env python3

import argparse
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from dateutil.parser import isoparse

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS


EXTRA_FIELD_KEYWORDS = [
    "precision",
    "recall",
    "f1",
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


def parse_time(value):
    if not value:
        return datetime.now(timezone.utc)

    dt = isoparse(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def as_float(value):
    if value is None or value == "":
        return None

    try:
        return float(value)
    except Exception:
        return None


def clean_tag(value):
    if value is None:
        return "unknown"
    text = str(value).strip()
    return text if text else "unknown"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", help="Run-Verzeichnis, z. B. runs/<RUN_ID>")
    parser.add_argument("--env-file", default=".env.influx")
    parser.add_argument("--url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--org", default=None)
    parser.add_argument("--bucket", default=None)
    args = parser.parse_args()

    load_env_file(Path(args.env_file))

    influx_url = args.url or os.environ.get("INFLUX_URL")
    influx_token = args.token or os.environ.get("INFLUX_TOKEN")
    influx_org = args.org or os.environ.get("INFLUX_ORG", "thade")
    influx_bucket = args.bucket or os.environ.get("INFLUX_BUCKET", "greenaidata")

    if not influx_url:
        raise SystemExit("Fehler: INFLUX_URL fehlt.")
    if not influx_token:
        raise SystemExit("Fehler: INFLUX_TOKEN fehlt.")

    run_dir = Path(args.run_dir)
    metadata_path = run_dir / "run_metadata.json"
    csv_path = run_dir / "metrics_rounds.csv"

    if not metadata_path.exists():
        raise SystemExit(f"Fehlt: {metadata_path}")
    if not csv_path.exists():
        raise SystemExit(f"Fehlt: {csv_path}")

    meta = json.loads(metadata_path.read_text(encoding="utf-8"))

    run_id = clean_tag(meta.get("run_id") or run_dir.name)
    strategy = clean_tag(meta.get("strategy"))
    dataset = clean_tag(meta.get("dataset"))
    model = clean_tag(meta.get("model"))

    # Zeitstempel des Runs verwenden, damit die Punkte zeitlich zum Experiment gehören.
    point_time = parse_time(meta.get("end_time") or meta.get("start_time"))

    points = []

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise SystemExit("CSV enthält keine Spalten.")

        extra_fields = [
            col for col in reader.fieldnames
            if any(keyword in col.lower() for keyword in EXTRA_FIELD_KEYWORDS)
        ]

        if not extra_fields:
            print("Keine Extra-Metriken mit precision/recall/f1 in metrics_rounds.csv gefunden.")
            return

        for row in reader:
            round_id = clean_tag(row.get("round"))

            point = (
                Point("fl_round_metrics")
                .tag("run_id", run_id)
                .tag("strategy", strategy)
                .tag("dataset", dataset)
                .tag("model", model)
                .tag("round", round_id)
                .time(point_time, WritePrecision.NS)
            )

            field_count = 0

            for field in extra_fields:
                value = as_float(row.get(field))
                if value is None:
                    continue

                # Alles als Float schreiben, um int/float-Schema-Probleme zu vermeiden.
                point = point.field(field, float(value))
                field_count += 1

            if field_count > 0:
                points.append(point)

    if not points:
        print("Keine Punkte zum Schreiben gefunden.")
        return

    with InfluxDBClient(url=influx_url, token=influx_token, org=influx_org) as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=influx_bucket, org=influx_org, record=points)

    print("Extra Round-Metriken nach InfluxDB geschrieben.")
    print(f"Measurement: fl_round_metrics")
    print(f"Run ID: {run_id}")
    print(f"Points: {len(points)}")
    print("Fields:")
    for field in extra_fields:
        print(f" - {field}")


if __name__ == "__main__":
    main()
