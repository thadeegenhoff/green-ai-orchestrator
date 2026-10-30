import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from energy_integration import aggregate_energy_results, calculate_energy


class EnergyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.end = self.start + timedelta(seconds=3600)

    def test_missing_and_single_sample_are_unavailable(self):
        for samples in [[], [(self.start, 20.0)]]:
            result = calculate_energy(samples, self.start, self.end)
            self.assertFalse(result["valid"])
            self.assertIsNone(result["energy_wh"])
            self.assertEqual(result["measurement_status"], "insufficient_samples")

    def test_real_zero_power_is_valid(self):
        result = calculate_energy([(self.start, 0), (self.end, 0)], self.start, self.end)
        self.assertTrue(result["valid"])
        self.assertEqual(result["energy_wh"], 0.0)

    def test_trapezoid_and_endpoint_extension(self):
        samples = [(self.start + timedelta(seconds=600), 10),
                   (self.end - timedelta(seconds=600), 30)]
        result = calculate_energy(samples, self.start, self.end, idle_power_w=5)
        self.assertAlmostEqual(result["energy_wh"], 20.0)
        self.assertAlmostEqual(result["net_energy_wh"], 15.0)

    def test_invalid_input_is_not_silently_integrated(self):
        cases = [
            [(self.start, float("nan")), (self.end, 10)],
            [(self.end, 10), (self.start, 20)],
            [(self.start, 10), (self.start, 20)],
            [(self.start - timedelta(seconds=1), 10), (self.end, 20)],
        ]
        for samples in cases:
            self.assertFalse(calculate_energy(samples, self.start, self.end)["valid"])
        self.assertFalse(calculate_energy([], self.end, self.start)["valid"])

    def test_incomplete_total_is_not_a_partial_sum(self):
        good = calculate_energy([(self.start, 10), (self.end, 10)], self.start, self.end)
        bad = calculate_energy([], self.start, self.end)
        total = aggregate_energy_results([good, bad])
        self.assertFalse(total["valid"])
        self.assertIsNone(total["total_energy_wh"])
        self.assertEqual(total["invalid_power_node_count"], 1)

    def test_complete_total_and_missing_baseline(self):
        one = calculate_energy([(self.start, 10), (self.end, 10)], self.start, self.end, 2)
        two = calculate_energy([(self.start, 20), (self.end, 20)], self.start, self.end)
        total = aggregate_energy_results([one, two])
        self.assertTrue(total["valid"])
        self.assertEqual(total["total_energy_wh"], 30.0)
        self.assertIsNone(total["total_net_energy_wh"])


class FakePoint:
    def __init__(self, name):
        self.name, self.fields = name, {}

    def tag(self, *args):
        return self

    def field(self, name, value):
        self.fields[name] = value
        return self

    def time(self, *args):
        return self


def load_exporter(filename):
    """Mock only the external SDK and time parser; run the real export logic."""
    sdk = types.ModuleType("influxdb_client")
    sdk.InfluxDBClient = object
    sdk.Point = FakePoint
    sdk.WritePrecision = types.SimpleNamespace(NS="ns")
    client = types.ModuleType("influxdb_client.client")
    write_api = types.ModuleType("influxdb_client.client.write_api")
    write_api.SYNCHRONOUS = object()
    dateutil = types.ModuleType("dateutil")
    parser = types.ModuleType("dateutil.parser")
    parser.isoparse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    modules = {"influxdb_client": sdk, "influxdb_client.client": client,
               "influxdb_client.client.write_api": write_api,
               "dateutil": dateutil, "dateutil.parser": parser}
    spec = importlib.util.spec_from_file_location(filename[:-3], SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    env = {key: "test" for key in ["INFLUX_URL", "INFLUX_ORG", "INFLUX_BUCKET", "INFLUX_TOKEN"]}
    with patch.dict(sys.modules, modules), patch.dict("os.environ", env):
        spec.loader.exec_module(module)
    return module


class ExportTests(unittest.TestCase):
    def test_both_exporters_mark_missing_data_and_omit_energy(self):
        for filename in ["energy_summary.py", "energy_summary_inference.py"]:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as folder:
                run = Path(folder)
                meta = {"run_id": "test-run", "start_time": "2026-01-01T00:00:00Z",
                        "end_time": "2026-01-01T00:00:10Z", "nodes": [{"node": "worker"}]}
                (run / "run_metadata.json").write_text(json.dumps(meta))
                perf = {"benchmark_start_time": meta["start_time"], "benchmark_end_time": meta["end_time"],
                        "inference_node": "worker", "benchmark_node": "worker",
                        "requests": 10, "successful_requests": 10}
                summary = run / "inference_summary.json"
                summary.write_text(json.dumps(perf))
                records = []

                class Client:
                    def __init__(self, **kwargs):
                        pass
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        pass
                    def write_api(self, **kwargs):
                        return self
                    def write(self, **kwargs):
                        records.append(kwargs["record"])

                module = load_exporter(filename)
                argv = [filename, str(run)] if filename == "energy_summary.py" else [
                    filename, str(summary), "--train-run-dir", str(run),
                    "--url", "http://unused", "--token", "test"]
                with patch.object(module, "InfluxDBClient", Client), \
                     patch.object(module, "query_power_samples", return_value=[]), \
                     patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as exit_result:
                        module.main()
                self.assertEqual(exit_result.exception.code, 2)
                for record in records:
                    self.assertNotIn("energy_wh", record.fields)
                    self.assertNotIn("total_energy_wh", record.fields)
                    flag = record.fields.get("energy_valid", record.fields.get("valid"))
                    self.assertEqual(flag, 0.0)
                output = run / ("energy_summary.json" if filename == "energy_summary.py" else "inference_energy_summary.json")
                result = json.loads(output.read_text())
                total = result.get("total", result)
                self.assertFalse(total["valid"])
                self.assertIsNone(total["total_energy_wh"])

    def test_both_exporters_keep_valid_zero_and_positive_energy(self):
        for filename in ["energy_summary.py", "energy_summary_inference.py"]:
            for power in [0.0, 20.0]:
                with self.subTest(filename=filename, power=power), tempfile.TemporaryDirectory() as folder:
                    run = Path(folder)
                    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
                    end = start + timedelta(seconds=10)
                    meta = {"run_id": "test-run", "start_time": start.isoformat(),
                            "end_time": end.isoformat(), "nodes": [{"node": "worker"}]}
                    (run / "run_metadata.json").write_text(json.dumps(meta))
                    summary = run / "inference_summary.json"
                    summary.write_text(json.dumps({
                        "benchmark_start_time": start.isoformat(), "benchmark_end_time": end.isoformat(),
                        "inference_node": "worker", "benchmark_node": "worker", "successful_requests": 10,
                    }))
                    records = []

                    class Client:
                        def __init__(self, **kwargs):
                            pass
                        def __enter__(self):
                            return self
                        def __exit__(self, *args):
                            pass
                        def write_api(self, **kwargs):
                            return self
                        def write(self, **kwargs):
                            records.append(kwargs["record"])

                    module = load_exporter(filename)
                    argv = [filename, str(run)] if filename == "energy_summary.py" else [
                        filename, str(summary), "--train-run-dir", str(run),
                        "--url", "http://unused", "--token", "test"]
                    with patch.object(module, "InfluxDBClient", Client), \
                         patch.object(module, "query_power_samples", return_value=[(start, power), (end, power)]), \
                         patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                        module.main()
                    output = run / ("energy_summary.json" if filename == "energy_summary.py" else "inference_energy_summary.json")
                    result = json.loads(output.read_text())
                    total = result.get("total", result)
                    self.assertTrue(total["valid"])
                    self.assertAlmostEqual(total["total_energy_wh"], power * 10 / 3600)
                    self.assertTrue(any("energy_wh" in point.fields for point in records))


if __name__ == "__main__":
    unittest.main()
