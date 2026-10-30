import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ControllerAndReportTests(unittest.TestCase):
    def test_image_override_reaches_each_generated_client(self):
        controller = load_module("controller", ROOT / "run_experiment.py")
        config = yaml.safe_load((ROOT / "experiments/quickstart_fedavg_cifar10_iid_20r_ep2_b64.yaml").read_text())
        env = {"APP_IMAGE": "example.invalid/greenai:test", "K8S_RUN_ID": "test-run", "RUN_ID": "test-run"}
        generated = controller.generate_client_workloads(config["kubernetes_runtime"], config["project"]["run_config"], "flwr", env)
        manifests = list(yaml.safe_load_all(generated))
        workloads = [doc for doc in manifests if doc["kind"] == "StatefulSet"]
        self.assertEqual(len(workloads), 4)
        for doc in workloads:
            images = [c["image"] for c in doc["spec"]["template"]["spec"]["containers"]]
            self.assertIn(env["APP_IMAGE"], images)
            self.assertFalse(any(image.startswith("thadegreen/") for image in images))

    def test_report_keeps_invalid_energy_separate_from_training_validity(self):
        report = load_module("generate_report", ROOT / "scripts/generate_report.py")
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            (run / "run_metadata.json").write_text(json.dumps({"run_id": "test"}))
            (run / "validation.json").write_text(json.dumps({"valid": True}))
            (run / "energy_summary.json").write_text(json.dumps({
                "valid": False, "measurement_status": "incomplete_device_measurements", "total_energy_wh": None,
                "nodes": [{"node": "worker", "device": "plug", "energy_wh": None,
                           "sample_count": 0, "valid": False, "measurement_status": "insufficient_samples"}],
            }))
            with patch.object(sys, "argv", ["generate_report.py", str(run)]), contextlib.redirect_stdout(io.StringIO()):
                report.main()
            summary = json.loads((run / "summary.json").read_text())
            self.assertTrue(summary["valid"])
            self.assertFalse(summary["energy_valid"])
            self.assertIsNone(summary["total_energy_wh"])
            self.assertIn("insufficient_samples", (run / "energy_nodes.csv").read_text())


if __name__ == "__main__":
    unittest.main()
