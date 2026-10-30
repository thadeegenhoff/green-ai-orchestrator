# Green AI Orchestrator

This prototype runs configurable Flower training on a local K3s/Kubernetes pool, retains the final model, deploys HTTP inference and joins AI metrics with physical-device energy readings. The independent measurement services live in [green-ai-sustainability](https://github.com/thadeegenhoff/green-ai-sustainability). If that link is hidden in an anonymised review mirror, use the separate measurement-component link supplied with the manuscript.

The repositories contain implementation code and example configurations. Original experiment databases, run directories and trained models are not included. A fresh execution produces new results; it does not reproduce the thesis numbers merely by using the same YAML.

## 1. Start the measurement services

Follow the companion repository's README first, including the Node-RED InfluxDB token configuration. Check the Shelly → MQTT → Node-RED → InfluxDB path before running an AI experiment.

The orchestrator needs the measurement host's reachable IP, not the Compose-internal name `influxdb`.

## 2. Install the controller and application

Use Linux, Python 3.11 or later supported by the dependencies, a working `kubectl` context and a ready K3s/Kubernetes cluster. Docker with Buildx is needed when building images. Internet access is needed for Python packages, images and the CIFAR-10 dataset.

For an anonymised review mirror, use its **Download repository as ZIP** control and open a terminal in the extracted directory. The clone command below applies to the public repository; skip the clone and `cd` lines when working from a downloaded snapshot.

```bash
git clone https://github.com/thadeegenhoff/green-ai-orchestrator.git
cd green-ai-orchestrator
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e quickstart-pytorch
python -m pip install influxdb-client python-dateutil pyyaml requests markdown fastapi uvicorn python-multipart pillow httpx
```

Flower is fixed to 1.29.0 to match the runtime images and application API. Simulation is optional (`pip install -e 'quickstart-pytorch[simulation]'`); it is not needed for the K3s deployment.

## 3. Configure InfluxDB and the Flower connection

```bash
cp .env.influx.example .env.influx
# Edit .env.influx to match the companion stack, then:
set -a
source .env.influx
set +a
curl --fail "$INFLUX_URL/health"
```

Use the same `INFLUX_ORG`, `INFLUX_BUCKET` and token as the measurement stack. `.env.influx` is ignored by Git.

Create a project-local Flower connection, avoiding changes to other Flower projects:

```bash
mkdir -p .flwr-local
cp configs/flower-config.example.toml .flwr-local/config.toml
export FLWR_HOME="$PWD/.flwr-local"
flwr config list
```

The connection uses `127.0.0.1:9093`. `run_experiment.py` opens the matching port-forward to the cluster's SuperLink. Keep `FLWR_HOME` set in the terminal that starts the run. If using a different local port, update both the connection file and `cluster_profiles/k3s_greenai.yaml`.

## 4. Adapt placement and physical-device mappings

```bash
kubectl get nodes --show-labels
kubectl get namespace flwr || kubectl create namespace flwr
CONTROL_NODE=your-control-node
kubectl label node "$CONTROL_NODE" greenai.local/fl-role=server greenai.local/workload-zone=control --overwrite
```

Before the first run, adapt all three configuration surfaces:

- `experiments/*.yaml`: `client_groups`, replica counts and node selectors. The supplied examples use the original worker hostnames. Match `kubernetes.io/hostname` selectors to your actual nodes, or use your own capability labels.
- `cluster_profiles/k3s_greenai.yaml`: namespace, rollouts, port-forward and `nodes.include_for_energy`. Existing examples use the `flwr` namespace, also present in the Kubernetes template.
- `node_mapping.json`: logical node → physical host/MQTT `device` tag, hardware metadata and optional measured idle-power baseline. VM energy is the physical host's energy. Replace the example baseline values with values from your own setup or remove them to omit net energy.

The control node needs the two labels above. Selectors for worker groups must also match existing node labels. The sum of client replicas must equal `num-partitions`; the controller fills the latter when omitted and rejects a mismatch.

## 5. Build and distribute the application image

`Dockerfile.greenai` builds the application directly from the official Flower SuperExec image and the source in this repository. It does not require the author's prebuilt application image.

```bash
export GREENAI_APP_IMAGE=ghcr.io/your-account/greenai:ecis-demo
# Log in to your chosen registry first.
docker buildx build --platform linux/amd64,linux/arm64 \
  -f Dockerfile.greenai -t "$GREENAI_APP_IMAGE" --push .
```

Use a registry that every selected K3s node can read, or configure the required registry credentials. A native-only local build is possible, but K3s uses its own container runtime: images must be imported on each selected node or pulled from a registry.

`GREENAI_APP_IMAGE` overrides the application image in the supplied experiment YAMLs for both ServerApp and ClientApps. The historical example tags remain in those files for reference. Supply the same chosen image as `IMAGE` for inference. The controller records the resolved image references in `run_metadata.json`.

Only the Flower version is constrained here. The remaining Python dependencies are not a complete historical lockfile; record the successful image digest and installed package versions for a fixed release.

## 6. Run a small training experiment

Start with a copy of an example adapted to your nodes and a small number of rounds:

```bash
python run_experiment.py \
  --config experiments/quickstart_fedavg_cifar10_iid_10r_flex.yaml \
  --profile cluster_profiles/k3s_greenai.yaml
```

`--profile` optionally overrides `cluster_profile` from the experiment YAML. Strategy, partitioning, rounds, epochs, batch size and client groups remain YAML parameters. The controller renders resources, waits for rollouts, starts Flower and retains outputs under `runs/<run_id>/`.

Expected artefacts include `experiment_config.yaml`, `cluster_profile.yaml`, `k8s-rendered.yaml`, logs, validation, metrics, `run_metadata.json`, energy results and `model/final_model.pt`. Model definition and preprocessing are in `quickstart-pytorch/pytorchexample/`.

## 7. Run controlled inference using that model

```bash
TRAIN_RUN_ID=replace-with-an-existing-run-id
export INFERENCE_NODE=pi5-8gb
export BENCHMARK_NODE=greenai-control
export IMAGE="$GREENAI_APP_IMAGE"
export SAMPLES=1000
export CONCURRENCY=4
bash scripts/run_inference_eval_k3s.sh "$TRAIN_RUN_ID" "inference-$(date -u +%Y%m%dT%H%M%SZ)"
```

Replace both node names with nodes from your cluster and use a new evaluation ID for each execution. The script loads `runs/<run_id>/model/final_model.pt`, creates the model ConfigMap, serving Deployment, Service and benchmark Job, copies the result files and writes metrics to InfluxDB. Outputs are retained under `runs/<run_id>/inference/<eval_id>/`. Larger models need a different storage adapter than the current ConfigMap approach.

## Energy results and missing data

Both energy exporters use trapezoidal integration of `power_w`. The first and last available powers are extended to the requested window boundaries. Net energy subtracts a configured idle baseline and is clipped at zero. Training uses the controller's start/end timestamps, including deployment/startup within that interval. Inference uses the benchmark timestamps, after health checks and warmup.

At least two finite observations at distinct times within a positive-duration window are required. Missing or invalid measurements produce `valid=0` and a `measurement_status`; their energy fields are unavailable rather than zero. Real zero-power observations remain valid. A total is unavailable if any requested device has invalid data; net totals additionally require a baseline for every device. Exporters save structured JSON and return exit code 2 on incomplete measurements, while preserving the available performance outputs. Training reports retain the separate `energy_valid` status.

These checks do not establish calibration, clock accuracy or adequate temporal coverage. Large sample gaps are not automatically rejected. Original measurements are not changed by this code update. Do not reuse historical run/evaluation IDs when generating new results.

## Validation

```bash
python -m unittest discover -s tests -v
python -m compileall -q run_experiment.py scripts quickstart-pytorch/pytorchexample
bash -n scripts/run_inference_eval_k3s.sh
```

The tests cover integration arithmetic, missing observations, incomplete totals and export behavior without requiring K3s or InfluxDB. A physical cluster run remains a separate validation step.

## Scope and licensing

This is a locally controlled research demonstrator. Flower communication uses the existing insecure configuration; a production deployment needs a separate security design. No independent reproduction or lifecycle environmental benefit is established by publishing the code.

## License

Copyright 2026 Thade Egenhoff.

Except where otherwise noted, the original code, configuration files and documentation in this repository are licensed under the [Apache License, Version 2.0](LICENSE) (`Apache-2.0`). See [NOTICE](NOTICE) for attribution information.

The Flower quickstart's existing Apache-2.0 licence is retained under [`quickstart-pytorch/LICENSE`](quickstart-pytorch/LICENSE). Third-party software, container images and datasets retain their respective licences.
