#!/usr/bin/env python3

import json
import os
import sys
from pathlib import Path
from datetime import timezone
from dateutil.parser import isoparse

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from energy_integration import aggregate_energy_results, calculate_energy


INFLUX_URL = os.environ["INFLUX_URL"]
INFLUX_ORG = os.environ["INFLUX_ORG"]
INFLUX_BUCKET = os.environ["INFLUX_BUCKET"]
INFLUX_TOKEN = os.environ["INFLUX_TOKEN"]

POWER_MEASUREMENT = os.environ.get("POWER_MEASUREMENT", "shelly_plug")
POWER_FIELD = os.environ.get("POWER_FIELD", "power_w")
POWER_NODE_TAG = os.environ.get("POWER_NODE_TAG", "device")

POWER_GENERATION_TAG = os.environ.get("POWER_GENERATION_TAG", "")
POWER_GENERATION_VALUE = os.environ.get("POWER_GENERATION_VALUE", "")


def parse_time(value):
    dt = isoparse(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def flux_escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def build_generation_filter():
    if POWER_GENERATION_TAG and POWER_GENERATION_VALUE:
        return f'  |> filter(fn: (r) => r["{flux_escape(POWER_GENERATION_TAG)}"] == "{flux_escape(POWER_GENERATION_VALUE)}")\n'
    return ""


def query_power_samples(client, start_time, end_time, power_tag_value):
    start = start_time.isoformat().replace("+00:00", "Z")
    stop = end_time.isoformat().replace("+00:00", "Z")
    generation_filter = build_generation_filter()

    query = f'''
from(bucket: "{flux_escape(INFLUX_BUCKET)}")
  |> range(start: time(v: "{start}"), stop: time(v: "{stop}"))
  |> filter(fn: (r) => r._measurement == "{flux_escape(POWER_MEASUREMENT)}")
  |> filter(fn: (r) => r._field == "{flux_escape(POWER_FIELD)}")
  |> filter(fn: (r) => r["{flux_escape(POWER_NODE_TAG)}"] == "{flux_escape(power_tag_value)}")
{generation_filter}  |> keep(columns: ["_time", "_value"])
'''

    tables = client.query_api().query(query, org=INFLUX_ORG)

    samples = []
    for table in tables:
        for record in table.records:
            samples.append((record.get_time(), float(record.get_value())))

    samples.sort(key=lambda x: x[0])
    return samples


def main():
    if len(sys.argv) != 2:
        print("Nutzung: python energy_summary.py runs/<RUN_ID>")
        sys.exit(1)

    run_dir = Path(sys.argv[1])
    metadata_path = run_dir / "run_metadata.json"

    if not metadata_path.exists():
        print(f"Fehlt: {metadata_path}")
        sys.exit(1)

    with metadata_path.open("r", encoding="utf-8") as f:
        meta = json.load(f)

    run_id = meta["run_id"]
    strategy = meta.get("strategy", "unknown")
    dataset = meta.get("dataset", "unknown")
    model = meta.get("model", "unknown")

    start_time = parse_time(meta["start_time"])
    end_time = parse_time(meta["end_time"])

    node_results = []

    with InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG) as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)

        for node_info in meta.get("nodes", []):
            node = node_info["node"]
            power_tag_value = node_info.get("power_tag_value", node)
            device_class = node_info.get("device_class", "unknown")
            role = node_info.get("role", "unknown")
            performance_tier = node_info.get("performance_tier", "unknown")
            accelerator = node_info.get("accelerator", "none")
            gpu = str(node_info.get("gpu", False)).lower()
            arch = node_info.get("arch", "unknown")
            idle_power_w = node_info.get("idle_power_w")

            samples = query_power_samples(
                client=client,
                start_time=start_time,
                end_time=end_time,
                power_tag_value=power_tag_value
            )

            summary = calculate_energy(
                samples=samples,
                start_time=start_time,
                end_time=end_time,
                idle_power_w=idle_power_w
            )

            node_results.append({"node": node, "device": power_tag_value, **summary})

            point = (
                Point("fl_energy_summary")
                .tag("run_id", run_id)
                .tag("strategy", strategy)
                .tag("dataset", dataset)
                .tag("model", model)
                .tag("node", node)
                .tag("device", power_tag_value)
                .tag("device_class", device_class)
                .tag("role", role)
                .tag("performance_tier", performance_tier)
                .tag("accelerator", accelerator)
                .tag("gpu", gpu)
                .tag("arch", arch)
                .field("duration_s", float(summary["duration_s"]))
                .field("sample_count", int(summary["sample_count"]))
                .field("valid", float(summary["valid"]))
                .field("measurement_status", summary["measurement_status"])
                .time(end_time, WritePrecision.NS)
            )

            for field in ["energy_wh", "net_energy_wh", "avg_power_w", "max_power_w"]:
                if summary[field] is not None:
                    point.field(field, float(summary[field]))

            write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)

            if summary["valid"]:
                print(
                    f"{node} / device={power_tag_value}: "
                    f"{summary['energy_wh']:.4f} Wh, "
                    f"avg={summary['avg_power_w']:.2f} W, "
                    f"max={summary['max_power_w']:.2f} W, "
                    f"samples={summary['sample_count']}"
                )
            else:
                print(f"{node} / device={power_tag_value}: INVALID "
                      f"({summary['measurement_status']}), samples={summary['sample_count']}")

        totals = aggregate_energy_results(node_results)
        run_point = (
            Point("fl_run_summary")
            .tag("run_id", run_id)
            .tag("strategy", strategy)
            .tag("dataset", dataset)
            .tag("model", model)
            .field("energy_valid", float(totals["valid"]))
            .field("measurement_status", totals["measurement_status"])
            .field("valid_power_node_count", totals["valid_power_node_count"])
            .field("invalid_power_node_count", totals["invalid_power_node_count"])
            .field("duration_s", float((end_time - start_time).total_seconds()))
            .time(end_time, WritePrecision.NS)
        )

        for field in ["total_energy_wh", "total_net_energy_wh"]:
            if totals[field] is not None:
                run_point.field(field, float(totals[field]))

        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=run_point)

    print("---")
    print(f"Run: {run_id}")
    result = {"schema_version": 1, "run_id": run_id, "nodes": node_results, **totals}
    (run_dir / "energy_summary.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if totals["valid"]:
        print(f"Total energy: {totals['total_energy_wh']:.4f} Wh")
    else:
        print("Total energy: unavailable (incomplete device measurements)")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
