#!/usr/bin/env python3

import argparse
import json
import os
from datetime import timezone
from pathlib import Path

from dateutil.parser import isoparse
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from energy_integration import aggregate_energy_results, calculate_energy


PERFORMANCE_FIELDS = [
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
    dt = isoparse(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def flux_escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def clean_tag(value):
    if value is None:
        return "unknown"
    text = str(value).strip()
    return text if text else "unknown"


def as_float(value, default=None):
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


def tag_from_float(value, default="unknown"):
    number = as_float(value)
    if number is None:
        return default
    if float(number).is_integer():
        return str(int(number))
    return str(number)


def build_generation_filter(tag, value):
    if tag and value:
        return f'  |> filter(fn: (r) => r["{flux_escape(tag)}"] == "{flux_escape(value)}")\n'
    return ""


def query_power_samples(
    client,
    org,
    bucket,
    start_time,
    end_time,
    power_tag_value,
    power_measurement,
    power_field,
    power_node_tag,
    power_generation_tag,
    power_generation_value,
):
    start = start_time.isoformat().replace("+00:00", "Z")
    stop = end_time.isoformat().replace("+00:00", "Z")
    generation_filter = build_generation_filter(power_generation_tag, power_generation_value)

    query = f'''
from(bucket: "{flux_escape(bucket)}")
  |> range(start: time(v: "{start}"), stop: time(v: "{stop}"))
  |> filter(fn: (r) => r._measurement == "{flux_escape(power_measurement)}")
  |> filter(fn: (r) => r._field == "{flux_escape(power_field)}")
  |> filter(fn: (r) => r["{flux_escape(power_node_tag)}"] == "{flux_escape(power_tag_value)}")
{generation_filter}  |> keep(columns: ["_time", "_value"])
'''

    tables = client.query_api().query(query, org=org)

    samples = []
    for table in tables:
        for record in table.records:
            samples.append((record.get_time(), float(record.get_value())))

    samples.sort(key=lambda x: x[0])
    return samples


def node_entry_from_mapping(node_name, node_mapping):
    mapping = node_mapping.get(node_name, {}) or {}

    entry = {
        "node": node_name,
        "device_class": mapping.get("device_class", "unknown"),
        "shelly": mapping.get("shelly", "unknown"),
        "mqtt_prefix": mapping.get("mqtt_prefix", node_name),
        "power_tag_value": mapping.get("power_tag_value", mapping.get("mqtt_prefix", node_name)),
        "role": mapping.get("role", "unknown"),
        "fl_role": mapping.get("fl_role", "unknown"),
        "workload_zone": mapping.get("workload_zone", "unknown"),
        "performance_tier": mapping.get("performance_tier", "unknown"),
        "accelerator": mapping.get("accelerator", "none"),
        "gpu": mapping.get("gpu", False),
        "arch": mapping.get("arch", "unknown"),
    }

    if "idle_power_w" in mapping:
        entry["idle_power_w"] = mapping["idle_power_w"]

    return entry


def infer_node_role(node, inference_node, benchmark_node):
    roles = []
    if node == inference_node:
        roles.append("inference")
    if node == benchmark_node:
        roles.append("benchmark")
    return "+".join(roles) if roles else "observed"


def add_float_fields(point, values):
    for key, value in values.items():
        number = as_float(value)
        if number is not None:
            point = point.field(key, float(number))
    return point


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("summary_json", help="Pfad zu inference_summary.json")
    parser.add_argument("--train-run-dir", required=True)
    parser.add_argument("--node-mapping", default="node_mapping.json")
    parser.add_argument("--env-file", default=".env.influx")

    parser.add_argument("--eval-id", default=None)
    parser.add_argument("--mode", default="local_http")
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--model", default="cnn")
    parser.add_argument("--protocol", default="http")
    parser.add_argument("--inference-node", default=None)
    parser.add_argument("--benchmark-node", default=None)
    parser.add_argument("--extra-node", action="append", default=[])

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

    power_measurement = os.environ.get("POWER_MEASUREMENT", "shelly_plug")
    power_field = os.environ.get("POWER_FIELD", "power_w")
    power_node_tag = os.environ.get("POWER_NODE_TAG", "device")
    power_generation_tag = os.environ.get("POWER_GENERATION_TAG", "")
    power_generation_value = os.environ.get("POWER_GENERATION_VALUE", "")

    summary_path = Path(args.summary_json)
    summary = read_json(summary_path)

    if not summary:
        raise SystemExit(f"Fehler: Summary konnte nicht gelesen werden: {summary_path}")

    if "benchmark_start_time" not in summary or "benchmark_end_time" not in summary:
        raise SystemExit(
            "Fehler: inference_summary.json enthält keine benchmark_start_time/benchmark_end_time. "
            "Bitte den Benchmark nach dem Patch erneut ausführen."
        )

    start_time = parse_time(summary["benchmark_start_time"])
    end_time = parse_time(summary["benchmark_end_time"])

    train_run_dir = Path(args.train_run_dir)
    train_meta = read_json(train_run_dir / "run_metadata.json", default={}) or {}

    train_run_id = clean_tag(summary.get("train_run_id") or train_run_dir.name)
    eval_id = clean_tag(args.eval_id or summary.get("eval_id") or summary_path.parent.name)

    dataset = clean_tag(args.dataset or summary.get("dataset") or train_meta.get("dataset") or "cifar10")
    model = clean_tag(args.model or train_meta.get("model") or "cnn")
    protocol = clean_tag(args.protocol or summary.get("protocol") or "http")
    mode = clean_tag(args.mode)

    inference_node = clean_tag(args.inference_node or summary.get("inference_node") or "greenai-control")
    benchmark_node = clean_tag(args.benchmark_node or summary.get("benchmark_node") or "greenai-control")

    concurrency = tag_from_float(summary.get("concurrency"), default="unknown")
    batch_size = tag_from_float(summary.get("batch_size", 1.0), default="1")

    requests = as_float(summary.get("requests"), 0.0) or 0.0
    successful_requests = as_float(summary.get("successful_requests"), 0.0) or 0.0

    node_mapping = read_json(Path(args.node_mapping), default={}) or {}

    node_names = []
    for node in [inference_node, benchmark_node] + list(args.extra_node):
        if node and node != "unknown" and node not in node_names:
            node_names.append(node)

    if not node_names:
        raise SystemExit("Fehler: Keine Nodes für Energieauswertung angegeben.")

    node_results = []

    duration_s = (end_time - start_time).total_seconds()
    duration_h = duration_s / 3600.0 if duration_s > 0 else 0.0

    with InfluxDBClient(url=influx_url, token=influx_token, org=influx_org) as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)

        for node_name in node_names:
            node_info = node_entry_from_mapping(node_name, node_mapping)

            node = node_info["node"]
            power_tag_value = node_info.get("power_tag_value", node)
            device_class = node_info.get("device_class", "unknown")
            shelly = node_info.get("shelly", "unknown")
            role = node_info.get("role", "unknown")
            fl_role = node_info.get("fl_role", "unknown")
            workload_zone = node_info.get("workload_zone", "unknown")
            performance_tier = node_info.get("performance_tier", "unknown")
            accelerator = node_info.get("accelerator", "none")
            gpu = str(node_info.get("gpu", False)).lower()
            arch = node_info.get("arch", "unknown")
            idle_power_w = node_info.get("idle_power_w")
            node_eval_role = infer_node_role(node, inference_node, benchmark_node)

            samples = query_power_samples(
                client=client,
                org=influx_org,
                bucket=influx_bucket,
                start_time=start_time,
                end_time=end_time,
                power_tag_value=power_tag_value,
                power_measurement=power_measurement,
                power_field=power_field,
                power_node_tag=power_node_tag,
                power_generation_tag=power_generation_tag,
                power_generation_value=power_generation_value,
            )

            energy = calculate_energy(
                samples=samples,
                start_time=start_time,
                end_time=end_time,
                idle_power_w=idle_power_w,
            )

            energy_j = energy["energy_wh"] * 3600.0 if energy["valid"] else None
            net_energy_j = (
                energy["net_energy_wh"] * 3600.0
                if energy["net_energy_wh"] is not None else None
            )
            joule_per_request = energy_j / successful_requests if energy_j is not None and successful_requests > 0 else None
            wh_per_1000_requests = energy["energy_wh"] / successful_requests * 1000.0 if energy["valid"] and successful_requests > 0 else None
            net_joule_per_request = net_energy_j / successful_requests if net_energy_j is not None and successful_requests > 0 else None
            net_wh_per_1000_requests = energy["net_energy_wh"] / successful_requests * 1000.0 if net_energy_j is not None and successful_requests > 0 else None

            node_result = {
                "node": node,
                "valid": energy["valid"],
                "measurement_status": energy["measurement_status"],
                "sample_count": energy["sample_count"],
                "node_eval_role": node_eval_role,
                "device": power_tag_value,
                "shelly": shelly,
                "duration_s": energy["duration_s"],
                "power_sample_count": energy["sample_count"],
                "energy_wh": energy["energy_wh"],
                "energy_j": energy_j,
                "net_energy_wh": energy["net_energy_wh"],
                "net_energy_j": net_energy_j,
                "avg_power_w": energy["avg_power_w"],
                "min_power_w": energy["min_power_w"],
                "max_power_w": energy["max_power_w"],
                "requests": requests,
                "successful_requests": successful_requests,
                "joule_per_request": joule_per_request,
                "wh_per_1000_requests": wh_per_1000_requests,
                "net_joule_per_request": net_joule_per_request,
                "net_wh_per_1000_requests": net_wh_per_1000_requests,
            }

            node_results.append(node_result)

            point = (
                Point("fl_inference_energy")
                .tag("eval_id", eval_id)
                .tag("train_run_id", train_run_id)
                .tag("mode", mode)
                .tag("dataset", dataset)
                .tag("model", model)
                .tag("protocol", protocol)
                .tag("node", node)
                .tag("node_eval_role", node_eval_role)
                .tag("device", clean_tag(power_tag_value))
                .tag("shelly", clean_tag(shelly))
                .tag("device_class", clean_tag(device_class))
                .tag("role", clean_tag(role))
                .tag("fl_role", clean_tag(fl_role))
                .tag("workload_zone", clean_tag(workload_zone))
                .tag("performance_tier", clean_tag(performance_tier))
                .tag("accelerator", clean_tag(accelerator))
                .tag("gpu", clean_tag(gpu))
                .tag("arch", clean_tag(arch))
                .tag("concurrency", clean_tag(concurrency))
                .tag("batch_size", clean_tag(batch_size))
                .field("duration_s", float(energy["duration_s"]))
                .field("power_sample_count", float(energy["sample_count"]))
                .field("requests", float(requests))
                .field("successful_requests", float(successful_requests))
                .field("valid", float(energy["valid"]))
                .field("measurement_status", energy["measurement_status"])
                .time(end_time, WritePrecision.NS)
            )

            point = add_float_fields(point, {
                key: node_result[key] for key in [
                    "energy_wh", "energy_j", "avg_power_w", "min_power_w", "max_power_w",
                    "joule_per_request", "wh_per_1000_requests", "net_energy_wh", "net_energy_j",
                    "net_joule_per_request", "net_wh_per_1000_requests",
                ]
            })

            write_api.write(bucket=influx_bucket, org=influx_org, record=point)

            if energy["valid"]:
                print(f"{node} / device={power_tag_value}: {energy['energy_wh']:.6f} Wh, "
                      f"avg={energy['avg_power_w']:.2f} W, samples={int(energy['sample_count'])}")
            else:
                print(f"{node} / device={power_tag_value}: INVALID "
                      f"({energy['measurement_status']}), samples={int(energy['sample_count'])}")

        totals = aggregate_energy_results(node_results)
        total_energy_wh = totals["total_energy_wh"]
        total_energy_j = total_energy_wh * 3600.0 if total_energy_wh is not None else None
        total_net_energy_wh = totals["total_net_energy_wh"]
        total_net_energy_j = total_net_energy_wh * 3600.0 if total_net_energy_wh is not None else None
        total_fields = {
            "duration_s": float(duration_s),
            "requests": float(requests),
            "successful_requests": float(successful_requests),
            "power_node_count": float(totals["power_node_count"]),
            "valid_power_node_count": float(totals["valid_power_node_count"]),
            "invalid_power_node_count": float(totals["invalid_power_node_count"]),
            "total_energy_wh": total_energy_wh,
            "total_energy_j": total_energy_j,
            "total_avg_power_w": total_energy_wh / duration_h if total_energy_wh is not None and duration_h > 0 else None,
            "total_joule_per_request": total_energy_j / successful_requests if total_energy_j is not None and successful_requests > 0 else None,
            "total_wh_per_1000_requests": total_energy_wh / successful_requests * 1000.0 if total_energy_wh is not None and successful_requests > 0 else None,
            "total_net_energy_wh": total_net_energy_wh,
            "total_net_energy_j": total_net_energy_j,
            "total_net_joule_per_request": total_net_energy_j / successful_requests if total_net_energy_j is not None and successful_requests > 0 else None,
            "total_net_wh_per_1000_requests": total_net_energy_wh / successful_requests * 1000.0 if total_net_energy_wh is not None and successful_requests > 0 else None,
            "valid": float(totals["valid"]),
        }

        common_tags = (
            Point("fl_inference_energy_total")
            .tag("eval_id", eval_id)
            .tag("train_run_id", train_run_id)
            .tag("mode", mode)
            .tag("dataset", dataset)
            .tag("model", model)
            .tag("protocol", protocol)
            .tag("inference_node", inference_node)
            .tag("benchmark_node", benchmark_node)
            .tag("concurrency", clean_tag(concurrency))
            .tag("batch_size", clean_tag(batch_size))
            .time(end_time, WritePrecision.NS)
        )

        common_tags.field("measurement_status", totals["measurement_status"])
        common_tags = add_float_fields(common_tags, total_fields)
        write_api.write(bucket=influx_bucket, org=influx_org, record=common_tags)

        eval_point = (
            Point("fl_inference_eval_summary")
            .tag("eval_id", eval_id)
            .tag("train_run_id", train_run_id)
            .tag("mode", mode)
            .tag("dataset", dataset)
            .tag("model", model)
            .tag("protocol", protocol)
            .tag("inference_node", inference_node)
            .tag("benchmark_node", benchmark_node)
            .tag("concurrency", clean_tag(concurrency))
            .tag("batch_size", clean_tag(batch_size))
            .time(end_time, WritePrecision.NS)
        )

        perf_values = {key: summary.get(key) for key in PERFORMANCE_FIELDS}
        eval_point.field("measurement_status", totals["measurement_status"])
        eval_point = add_float_fields(eval_point, perf_values)
        eval_point = add_float_fields(eval_point, total_fields)

        write_api.write(bucket=influx_bucket, org=influx_org, record=eval_point)

    output = {
        "schema_version": 1,
        "eval_id": eval_id,
        "train_run_id": train_run_id,
        "mode": mode,
        "dataset": dataset,
        "model": model,
        "protocol": protocol,
        "inference_node": inference_node,
        "benchmark_node": benchmark_node,
        "concurrency": concurrency,
        "batch_size": batch_size,
        "benchmark_start_time": start_time.isoformat().replace("+00:00", "Z"),
        "benchmark_end_time": end_time.isoformat().replace("+00:00", "Z"),
        "measurement_status": totals["measurement_status"],
        "total": total_fields,
        "nodes": node_results,
    }

    out_path = summary_path.parent / "inference_energy_summary.json"
    write_json(out_path, output)

    print("---")
    print(f"Eval ID: {eval_id}")
    print(f"Train Run ID: {train_run_id}")
    print(f"Duration: {duration_s:.2f} s")
    if totals["valid"]:
        print(f"Total energy: {total_energy_wh:.6f} Wh")
    else:
        print("Total energy: unavailable (incomplete device measurements)")
    print(f"Geschrieben: {out_path}")
    if not totals["valid"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
