#!/usr/bin/env python3

import csv
import html
import json
import re
import sys
from datetime import datetime
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


def read_csv_rows(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def parse_energy_log(path):
    path = Path(path)
    if not path.exists():
        return {
            "nodes": [],
            "total_energy_wh": None,
        }

    nodes = []
    total_energy_wh = None

    pattern = re.compile(
        r"^(?P<node>.*?) / device=(?P<device>.*?): "
        r"(?P<energy>[0-9.]+) Wh, "
        r"avg=(?P<avg>[0-9.]+) W, "
        r"max=(?P<max>[0-9.]+) W, "
        r"samples=(?P<samples>[0-9]+)"
    )

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = pattern.search(line.strip())
        if m:
            nodes.append({
                "node": m.group("node"),
                "device": m.group("device"),
                "energy_wh": float(m.group("energy")),
                "avg_power_w": float(m.group("avg")),
                "max_power_w": float(m.group("max")),
                "sample_count": int(m.group("samples")),
            })

        m = re.search(r"Total energy:\s+([0-9.]+)\s+Wh", line)
        if m:
            total_energy_wh = float(m.group(1))

    return {
        "nodes": nodes,
        "total_energy_wh": total_energy_wh,
    }


def write_energy_csv(path, energy):
    fieldnames = [
        "node",
        "device",
        "energy_wh",
        "avg_power_w",
        "max_power_w",
        "sample_count",
        "valid",
        "measurement_status",
    ]

    with Path(path).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in energy.get("nodes", []):
            writer.writerow({key: row.get(key) for key in fieldnames})


def md_table(headers, rows):
    out = []
    out.append("| " + " | ".join(headers) + " |")
    out.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        out.append("| " + " | ".join("" if value is None else str(value) for value in row) + " |")
    return "\n".join(out)


def markdown_to_html(markdown_text):
    try:
        import markdown
        body = markdown.markdown(markdown_text, extensions=["tables", "fenced_code"])
    except Exception:
        body = "<pre>" + html.escape(markdown_text) + "</pre>"

    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <title>Federated Learning Experiment Report</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      max-width: 1100px;
      margin: 40px auto;
      line-height: 1.55;
      padding: 0 24px;
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
      margin: 16px 0;
    }}
    th, td {{
      border: 1px solid #ccc;
      padding: 6px 8px;
      text-align: left;
    }}
    th {{
      background: #f2f2f2;
    }}
    code, pre {{
      background: #f6f6f6;
      padding: 2px 4px;
      border-radius: 4px;
    }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def main():
    if len(sys.argv) != 2:
        print("Nutzung: python scripts/generate_report.py runs/<RUN_ID>")
        sys.exit(1)

    run_dir = Path(sys.argv[1])

    metadata = load_json(run_dir / "run_metadata.json", default={}) or {}
    config = load_yaml(run_dir / "experiment_config.yaml", default={}) or {}
    metrics = load_json(run_dir / "flower_metrics.json", default={}) or {}
    validation = load_json(run_dir / "validation.json", default={}) or {}
    round_rows = read_csv_rows(run_dir / "metrics_rounds.csv")
    # Prefer structured data so invalid devices and unavailable totals survive
    # report generation. Retain the log parser for historical run directories.
    energy = load_json(run_dir / "energy_summary.json")
    if energy is None:
        energy = parse_energy_log(run_dir / "energy_summary.log")

    write_energy_csv(run_dir / "energy_nodes.csv", energy)

    experiment = config.get("experiment", {})
    project = config.get("project", {})
    run_config = project.get("run_config", {})

    final_accuracy = None
    final_loss = None

    last_round = metrics.get("last_round_seen")
    if last_round is not None:
        round_data = metrics.get("rounds", {}).get(str(last_round), {})
        final_accuracy = round_data.get("server_accuracy")
        final_loss = round_data.get("server_loss")

    summary = {
        "run_id": metadata.get("run_id"),
        "k8s_run_id": metadata.get("k8s_run_id"),
        "flower_run_id": metrics.get("flower_run_id"),
        "status": metadata.get("status"),
        "valid": validation.get("valid"),
        "strategy": experiment.get("strategy"),
        "dataset": experiment.get("dataset"),
        "split": experiment.get("split"),
        "model": experiment.get("model"),
        "requested_num_server_rounds": validation.get("requested_num_server_rounds"),
        "actual_num_rounds": metrics.get("actual_num_rounds"),
        "last_round_seen": metrics.get("last_round_seen"),
        "local_epochs": run_config.get("local-epochs"),
        "batch_size": run_config.get("batch-size"),
        "learning_rate": run_config.get("learning-rate"),
        "strategy_duration_s": metrics.get("strategy_duration_s"),
        "final_accuracy": final_accuracy,
        "final_loss": final_loss,
        "total_energy_wh": energy.get("total_energy_wh"),
        "energy_valid": energy.get("valid"),
        "energy_measurement_status": energy.get("measurement_status"),
        "train_failures_total": metrics.get("train_failures_total"),
        "evaluate_failures_total": metrics.get("evaluate_failures_total"),
    }

    write_json(run_dir / "summary.json", summary)

    lines = []

    lines.append("# Federated Learning Experiment Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")

    lines.append("## 1. Experiment Summary")
    lines.append("")
    lines.append(md_table(
        ["Field", "Value"],
        [
            ["Run ID", summary["run_id"]],
            ["Kubernetes Run ID", summary["k8s_run_id"]],
            ["Flower Run ID", summary["flower_run_id"]],
            ["Status", summary["status"]],
            ["Valid", summary["valid"]],
            ["Strategy", summary["strategy"]],
            ["Dataset", summary["dataset"]],
            ["Split", summary["split"]],
            ["Model", summary["model"]],
            ["Requested rounds", summary["requested_num_server_rounds"]],
            ["Actual rounds", summary["actual_num_rounds"]],
            ["Last round seen", summary["last_round_seen"]],
            ["Local epochs", summary["local_epochs"]],
            ["Batch size", summary["batch_size"]],
            ["Learning rate", summary["learning_rate"]],
            ["Strategy duration seconds", summary["strategy_duration_s"]],
            ["Final accuracy", summary["final_accuracy"]],
            ["Final loss", summary["final_loss"]],
            ["Total energy Wh", summary["total_energy_wh"]],
        ],
    ))
    lines.append("")

    lines.append("## 2. Validation")
    lines.append("")
    validation_rows = []
    for check in validation.get("checks", []):
        validation_rows.append([
            check.get("name"),
            check.get("expected"),
            check.get("actual"),
            "OK" if check.get("ok") else "FAILED",
        ])
    lines.append(md_table(["Check", "Expected", "Actual", "Result"], validation_rows))
    lines.append("")

    lines.append("## 3. Training Metrics")
    lines.append("")
    if round_rows:
        rows = []
        for row in round_rows:
            rows.append([
                row.get("round"),
                row.get("train_loss"),
                row.get("eval_loss"),
                row.get("eval_acc"),
                row.get("server_accuracy"),
                row.get("server_loss"),
                row.get("train_failures"),
                row.get("evaluate_failures"),
            ])
        lines.append(md_table(
            [
                "Round",
                "Train loss",
                "Eval loss",
                "Eval acc",
                "Server accuracy",
                "Server loss",
                "Train failures",
                "Evaluate failures",
            ],
            rows,
        ))
    else:
        lines.append("No round metrics available.")
    lines.append("")

    lines.append("## 4. Energy Results")
    lines.append("")
    if energy.get("nodes"):
        rows = []
        for node in energy["nodes"]:
            rows.append([
                node.get("node"),
                node.get("device"),
                node.get("energy_wh"),
                node.get("avg_power_w"),
                node.get("max_power_w"),
                node.get("sample_count"),
                node.get("valid"),
                node.get("measurement_status"),
            ])
        lines.append(md_table(
            ["Node", "Device", "Energy Wh", "Avg W", "Max W", "Samples", "Energy valid", "Measurement status"],
            rows,
        ))
        lines.append("")
        if energy.get("total_energy_wh") is None:
            lines.append("Total energy unavailable: device measurements are incomplete or invalid.")
        else:
            lines.append(f"Total energy: **{energy.get('total_energy_wh')} Wh**")
    else:
        lines.append("No energy results available.")
    lines.append("")

    lines.append("## 5. Infrastructure")
    lines.append("")
    pods = metadata.get("pods", [])
    if pods:
        pod_rows = []
        for pod in pods:
            labels = pod.get("labels", {})
            pod_rows.append([
                pod.get("pod"),
                pod.get("node"),
                pod.get("phase"),
                labels.get("fl_role"),
                labels.get("node_target"),
            ])
        lines.append(md_table(
            ["Pod", "Node", "Phase", "FL role", "Node target"],
            pod_rows,
        ))
    else:
        lines.append("No pod metadata available.")
    lines.append("")

    lines.append("## 6. Run Configuration")
    lines.append("")
    lines.append("```yaml")
    lines.append(yaml.safe_dump(config, sort_keys=False, allow_unicode=True).strip())
    lines.append("```")
    lines.append("")

    lines.append("## 7. Interpretation")
    lines.append("")
    if validation.get("valid"):
        lines.append(
            "Der Lauf wurde erfolgreich validiert. Die gewünschte Anzahl an Server-Runden wurde erreicht, "
            "Flower lieferte finale Ergebnisse und es wurden keine Client-Fehler in den aggregierten Trainings- "
            "oder Evaluationsschritten erkannt."
        )
    else:
        lines.append(
            "Der Lauf ist nicht vollständig valide. Für die Masterarbeit sollte dieser Run nur zur Fehlersuche "
            "oder als technischer Testlauf verwendet werden, nicht als finales Experiment."
        )

    report_md = "\n".join(lines)
    (run_dir / "report.md").write_text(report_md, encoding="utf-8")

    report_html = markdown_to_html(report_md)
    (run_dir / "report.html").write_text(report_html, encoding="utf-8")

    print(f"Report geschrieben: {run_dir / 'report.md'}")
    print(f"Report geschrieben: {run_dir / 'report.html'}")
    print(f"Summary geschrieben: {run_dir / 'summary.json'}")


if __name__ == "__main__":
    main()
