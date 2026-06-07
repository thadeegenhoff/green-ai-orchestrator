#!/usr/bin/env python3

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


def calculate_energy(samples, start_time, end_time, idle_power_w=None):
    duration_s = (end_time - start_time).total_seconds()
    duration_h = duration_s / 3600.0

    if len(samples) < 2 or duration_s <= 0:
        return {
            "duration_s": duration_s,
            "sample_count": len(samples),
            "energy_wh": 0.0,
            "net_energy_wh": None,
            "avg_power_w": 0.0,
            "max_power_w": 0.0
        }

    points = [(start_time, samples[0][1])] + samples + [(end_time, samples[-1][1])]

    energy_wh = 0.0

    for i in range(len(points) - 1):
        t1, p1 = points[i]
        t2, p2 = points[i + 1]

        delta_s = (t2 - t1).total_seconds()
        if delta_s <= 0:
            continue

        avg_power = (p1 + p2) / 2.0
        energy_wh += avg_power * delta_s / 3600.0

    avg_power_w = energy_wh / duration_h if duration_h > 0 else 0.0
    max_power_w = max(p for _, p in samples)

    net_energy_wh = None
    if idle_power_w is not None:
        net_energy_wh = max(0.0, energy_wh - float(idle_power_w) * duration_h)

    return {
        "duration_s": duration_s,
        "sample_count": len(samples),
        "energy_wh": energy_wh,
        "net_energy_wh": net_energy_wh,
        "avg_power_w": avg_power_w,
        "max_power_w": max_power_w
    }


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

    total_energy_wh = 0.0
    total_net_energy_wh = 0.0
    has_net = False

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

            total_energy_wh += summary["energy_wh"]

            if summary["net_energy_wh"] is not None:
                total_net_energy_wh += summary["net_energy_wh"]
                has_net = True

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
                .field("energy_wh", float(summary["energy_wh"]))
                .field("avg_power_w", float(summary["avg_power_w"]))
                .field("max_power_w", float(summary["max_power_w"]))
                .time(end_time, WritePrecision.NS)
            )

            if summary["net_energy_wh"] is not None:
                point.field("net_energy_wh", float(summary["net_energy_wh"]))

            write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)

            print(
                f"{node} / device={power_tag_value}: "
                f"{summary['energy_wh']:.4f} Wh, "
                f"avg={summary['avg_power_w']:.2f} W, "
                f"max={summary['max_power_w']:.2f} W, "
                f"samples={summary['sample_count']}"
            )

        run_point = (
            Point("fl_run_summary")
            .tag("run_id", run_id)
            .tag("strategy", strategy)
            .tag("dataset", dataset)
            .tag("model", model)
            .field("total_energy_wh", float(total_energy_wh))
            .field("duration_s", float((end_time - start_time).total_seconds()))
            .time(end_time, WritePrecision.NS)
        )

        if has_net:
            run_point.field("total_net_energy_wh", float(total_net_energy_wh))

        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=run_point)

    print("---")
    print(f"Run: {run_id}")
    print(f"Total energy: {total_energy_wh:.4f} Wh")


if __name__ == "__main__":
    main()
