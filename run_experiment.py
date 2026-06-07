#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import pty
import select
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


BERLIN_TZ = ZoneInfo("Europe/Berlin")


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def local_now_iso():
    return datetime.now(BERLIN_TZ).replace(microsecond=0).isoformat()


def safe_timestamp():
    return datetime.now(BERLIN_TZ).strftime("%Y-%m-%dT%H-%M-%S_%Z")


def k8s_safe_name(value, max_len=63):
    value = value.lower()
    value = re.sub(r"[^a-z0-9-]", "-", value)
    value = re.sub(r"-+", "-", value)
    value = value.strip("-")

    if len(value) <= max_len:
        return value

    suffix = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    prefix = value[: max_len - len(suffix) - 1].rstrip("-")
    return f"{prefix}-{suffix}"


def load_yaml(path):
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_yaml(path, data):
    path = Path(path)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def load_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    path = Path(path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def run_command(command, log_file, cwd=None, env=None, append=True):
    mode = "a" if append else "w"
    log_file = Path(log_file)

    printable = " ".join(str(x) for x in command)
    print(f"$ {printable}", flush=True)

    with log_file.open(mode, encoding="utf-8") as f:
        f.write(f"\n$ {printable}\n")
        f.flush()

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
            env=env,
            bufsize=1,
        )

        for line in process.stdout:
            print(line, end="", flush=True)
            f.write(line)
            f.flush()

        return process.wait()



def run_command_pty(command, log_file, cwd=None, env=None, append=True):
    """Run command through a pseudo-terminal.

    This is useful for commands like `flwr run --stream`, where output can be
    buffered when stdout is a normal pipe. The function prints live output to
    the terminal and writes the same output to the log file.
    """
    mode = "ab" if append else "wb"
    log_file = Path(log_file)

    printable = " ".join(str(x) for x in command)
    print(f"$ {printable}", flush=True)

    master_fd, slave_fd = pty.openpty()

    try:
        with log_file.open(mode) as f:
            f.write(f"\n$ {printable}\n".encode("utf-8"))
            f.flush()

            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd,
                env=env,
                close_fds=True,
            )

            os.close(slave_fd)
            slave_fd = None

            while True:
                ready, _, _ = select.select([master_fd], [], [], 0.2)

                if master_fd in ready:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        data = b""

                    if data:
                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
                        f.write(data)
                        f.flush()

                if process.poll() is not None:
                    # Drain remaining output
                    while True:
                        try:
                            data = os.read(master_fd, 4096)
                        except OSError:
                            break

                        if not data:
                            break

                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
                        f.write(data)
                        f.flush()

                    return process.wait()

    finally:
        if slave_fd is not None:
            try:
                os.close(slave_fd)
            except OSError:
                pass

        try:
            os.close(master_fd)
        except OSError:
            pass


def kubectl_json(args):
    result = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("kubectl Fehler:", result.stderr.strip(), flush=True)
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def collect_pods(namespace, label_selector):
    data = kubectl_json([
        "kubectl",
        "get",
        "pods",
        "-n",
        namespace,
        "-l",
        label_selector,
        "-o",
        "json",
    ])

    pods = []
    if not data:
        return pods

    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})

        pods.append({
            "pod": metadata.get("name"),
            "namespace": metadata.get("namespace"),
            "node": spec.get("nodeName"),
            "phase": status.get("phase"),
            "pod_ip": status.get("podIP"),
            "host_ip": status.get("hostIP"),
            "start_time": status.get("startTime"),
            "labels": metadata.get("labels", {}),
        })

    return pods


def node_entry_from_mapping(node_name, node_mapping):
    mapping = node_mapping.get(node_name, {})

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


def collect_nodes_from_pods(pods, node_mapping):
    used_node_names = sorted({
        pod["node"]
        for pod in pods
        if pod.get("node")
    })

    return [
        node_entry_from_mapping(node_name, node_mapping)
        for node_name in used_node_names
    ]


def add_included_nodes(nodes, include_nodes, node_mapping):
    existing = {node["node"] for node in nodes}

    for node_name in include_nodes:
        if node_name not in existing:
            nodes.append(node_entry_from_mapping(node_name, node_mapping))
            existing.add(node_name)

    return nodes


def render_template(template_path, output_path, env):
    template = Path(template_path).read_text(encoding="utf-8")

    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

    def replace_var(match):
        name = match.group(1)
        return env.get(name, match.group(0))

    rendered = pattern.sub(replace_var, template)
    Path(output_path).write_text(rendered, encoding="utf-8")


def format_run_config_value(value):
    """Format values for Flower CLI --run-config.

    Flower expects TOML-compatible values:
    - strings as "value"
    - bools as true/false
    - ints/floats as plain numbers
    """
    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)

    if isinstance(value, float):
        return str(value)

    if value is None:
        return '""'

    text = str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def build_run_config_string(run_config):
    parts = []
    for key, value in run_config.items():
        parts.append(f"{key}={format_run_config_value(value)}")
    return " ".join(parts)



def default_client_groups():
    """Fallback client groups preserving the old 4-client setup."""
    return [
        {
            "name": "cloud-gpu",
            "tier": "high",
            "zone": "cloud",
            "replicas": 1,
            "node_selector": {"kubernetes.io/hostname": "cloud-gpu"},
        },
        {
            "name": "old-laptop",
            "tier": "medium",
            "zone": "edge",
            "replicas": 1,
            "node_selector": {"kubernetes.io/hostname": "old-laptop"},
        },
        {
            "name": "rpi5-16gb",
            "tier": "medium",
            "zone": "edge",
            "replicas": 1,
            "node_selector": {"kubernetes.io/hostname": "pi5-16gb"},
        },
        {
            "name": "rpi5-8gb",
            "tier": "low",
            "zone": "edge",
            "replicas": 1,
            "node_selector": {"kubernetes.io/hostname": "pi5-8gb"},
        },
    ]


def get_client_groups(kubernetes_runtime):
    groups = kubernetes_runtime.get("client_groups", []) or []
    if groups:
        return groups
    return default_client_groups()


def client_group_workload_name(group):
    return k8s_safe_name(f"flower-client-{group.get('name', 'group')}", max_len=48)


def default_client_resources(tier):
    """Conservative resource defaults per performance tier."""
    defaults = {
        "high": {
            "supernode": {
                "requests": {"cpu": "200m", "memory": "256Mi", "ephemeral-storage": "512Mi"},
                "limits": {"cpu": "1000m", "memory": "1Gi", "ephemeral-storage": "2Gi"},
            },
            "clientapp": {
                "requests": {"cpu": "800m", "memory": "1536Mi", "ephemeral-storage": "4Gi"},
                "limits": {"cpu": "3000m", "memory": "6Gi", "ephemeral-storage": "10Gi"},
            },
        },
        "medium": {
            "supernode": {
                "requests": {"cpu": "150m", "memory": "256Mi", "ephemeral-storage": "512Mi"},
                "limits": {"cpu": "750m", "memory": "1Gi", "ephemeral-storage": "2Gi"},
            },
            "clientapp": {
                "requests": {"cpu": "500m", "memory": "1Gi", "ephemeral-storage": "3Gi"},
                "limits": {"cpu": "2000m", "memory": "4Gi", "ephemeral-storage": "8Gi"},
            },
        },
        "low": {
            "supernode": {
                "requests": {"cpu": "100m", "memory": "256Mi", "ephemeral-storage": "512Mi"},
                "limits": {"cpu": "500m", "memory": "768Mi", "ephemeral-storage": "2Gi"},
            },
            "clientapp": {
                "requests": {"cpu": "300m", "memory": "768Mi", "ephemeral-storage": "3Gi"},
                "limits": {"cpu": "1000m", "memory": "2Gi", "ephemeral-storage": "6Gi"},
            },
        },
    }
    return defaults.get(str(tier).lower(), defaults["medium"])


def deep_merge(base, override):
    result = json.loads(json.dumps(base))
    if not override:
        return result

    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value

    return result


def make_common_env(env, extra=None):
    values = [
        {"name": "PYTHONUNBUFFERED", "value": "1"},
        {"name": "RUN_ID", "value": env.get("RUN_ID", "")},
        {"name": "K8S_RUN_ID", "value": env.get("K8S_RUN_ID", "")},
        {"name": "STRATEGY", "value": env.get("STRATEGY", "")},
        {"name": "DATASET", "value": env.get("DATASET", "")},
        {"name": "MODEL_NAME", "value": env.get("MODEL_NAME", "")},
        {"name": "NUM_ROUNDS", "value": env.get("NUM_ROUNDS", "")},
    ]

    if extra:
        for key, value in extra.items():
            values.append({"name": str(key), "value": str(value)})

    values.extend([
        {"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
        {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
        {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
    ])

    return values


def generate_client_workloads(kubernetes_runtime, run_config, namespace, env):
    """Generate one StatefulSet per client group."""
    groups = get_client_groups(kubernetes_runtime)

    if not groups:
        return ""

    total_partitions = int(
        run_config.get(
            "num-partitions",
            sum(int(g.get("replicas", 1)) for g in groups),
        )
    )

    app_image = kubernetes_runtime.get("app_image", "thadegreen/quickstart-pytorch-flex:0.1")
    supernode_image = kubernetes_runtime.get("supernode_image", "flwr/supernode:1.29.0")

    docs = []
    partition_offset = 0

    for group in groups:
        name = client_group_workload_name(group)
        replicas = int(group.get("replicas", 1))
        tier = str(group.get("tier", group.get("performance_tier", "medium"))).lower()
        zone = str(group.get("zone", group.get("workload_zone", "edge"))).lower()
        group_name = str(group.get("name", name))
        selector = group.get("node_selector", {}) or {}

        resources_default = default_client_resources(tier)
        resources_cfg = group.get("resources", {}) or {}
        supernode_resources = deep_merge(resources_default["supernode"], resources_cfg.get("supernode"))
        clientapp_resources = deep_merge(resources_default["clientapp"], resources_cfg.get("clientapp"))

        offset = int(group.get("partition_offset", partition_offset))

        labels = {
            "app": "flower-client",
            "client_app": name,
            "run_id": env.get("K8S_RUN_ID", ""),
            "fl_role": "client",
            "greenai.ai/client-group": k8s_safe_name(group_name, max_len=40),
            "greenai.ai/performance-tier": tier,
            "greenai.ai/workload-zone": zone,
        }

        service_doc = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "clusterIP": "None",
                "selector": {"client_app": name},
                "ports": [
                    {
                        "name": "clientappio",
                        "port": 9094,
                        "targetPort": 9094,
                    }
                ],
            },
        }

        supernode_script = f"""ORDINAL="${{HOSTNAME##*-}}"
PARTITION_ID=$(({offset} + ORDINAL))

echo "Starting Flower SuperNode group={group_name} tier={tier} zone={zone} partition-id=${{PARTITION_ID}} num-partitions={total_partitions}"

exec flower-supernode \\
  --insecure \\
  --superlink flower-superlink.flwr.svc.cluster.local:9092 \\
  --node-config "partition-id=${{PARTITION_ID}} num-partitions={total_partitions}" \\
  --clientappio-api-address 0.0.0.0:9094 \\
  --isolation process
"""

        clientapp_script = """sleep 8
exec flower-superexec \\
  --insecure \\
  --plugin-type clientapp \\
  --appio-api-address 127.0.0.1:9094
"""

        pod_spec = {
            "nodeSelector": selector,
            "containers": [
                {
                    "name": "supernode",
                    "image": supernode_image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["/bin/sh", "-c"],
                    "args": [supernode_script],
                    "ports": [{"name": "clientappio", "containerPort": 9094}],
                    "env": make_common_env(
                        env,
                        {
                            "CLIENT_GROUP": group_name,
                            "PERFORMANCE_TIER": tier,
                            "WORKLOAD_ZONE": zone,
                            "PARTITION_OFFSET": offset,
                            "NUM_PARTITIONS": total_partitions,
                        },
                    ),
                    "resources": supernode_resources,
                },
                {
                    "name": "superexec-clientapp",
                    "image": app_image,
                    "imagePullPolicy": "Always",
                    "command": ["/bin/sh", "-c"],
                    "args": [clientapp_script],
                    "env": make_common_env(
                        env,
                        {
                            "CLIENT_GROUP": group_name,
                            "PERFORMANCE_TIER": tier,
                            "WORKLOAD_ZONE": zone,
                            "PARTITION_OFFSET": offset,
                            "NUM_PARTITIONS": total_partitions,
                        },
                    ),
                    "resources": clientapp_resources,
                },
            ],
        }

        tolerations = group.get("tolerations")
        if tolerations:
            pod_spec["tolerations"] = tolerations

        affinity = group.get("affinity")
        if affinity:
            pod_spec["affinity"] = affinity

        statefulset_doc = {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "serviceName": name,
                "replicas": replicas,
                "selector": {"matchLabels": {"client_app": name}},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": pod_spec,
                },
            },
        }

        docs.append(service_doc)
        docs.append(statefulset_doc)

        partition_offset = offset + replicas

    return "\n---\n".join(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True).strip()
        for doc in docs
    ) + "\n"


def build_rollouts(base_rollouts, kubernetes_runtime):
    """Use profile rollouts, but replace static client rollouts with dynamic StatefulSets."""
    groups = get_client_groups(kubernetes_runtime)

    rollouts = []
    for rollout in base_rollouts:
        name = rollout.get("name", "")
        if "flower-client" not in name:
            rollouts.append(rollout)

    for group in groups:
        rollouts.append({
            "name": f"statefulset/{client_group_workload_name(group)}",
            "timeout": str(group.get("rollout_timeout", "10m")),
        })

    return rollouts


def cleanup_dynamic_client_workloads(namespace, log_file, env):
    """Delete dynamically generated client workloads from previous runs."""
    for kind in ["statefulset", "deployment", "service", "pod"]:
        run_command(
            [
                "kubectl",
                "delete",
                kind,
                "-n",
                namespace,
                "-l",
                "app=flower-client",
                "--ignore-not-found=true",
            ],
            log_file=log_file,
            env=env,
        )



def cleanup_kubernetes(namespace, cleanup_cfg, log_file, env):
    for kind, key in [
        ("deployment", "deployments"),
        ("statefulset", "statefulsets"),
        ("pod", "pods"),
        ("service", "services"),
    ]:
        for name in cleanup_cfg.get(key, []):
            run_command(
                ["kubectl", "delete", kind, name, "-n", namespace, "--ignore-not-found=true"],
                log_file=log_file,
                env=env,
            )


def rollout_kubernetes(namespace, rollouts, log_file, env):
    for rollout in rollouts:
        name = rollout["name"]
        timeout = rollout.get("timeout", "10m")
        rc = run_command(
            ["kubectl", "rollout", "status", name, "-n", namespace, f"--timeout={timeout}"],
            log_file=log_file,
            env=env,
        )
        if rc != 0:
            return rc
    return 0


def start_port_forward(namespace, service, local_port, remote_port, log_path, env, ready_timeout_s=30):
    log_path = Path(log_path)
    f = log_path.open("w", encoding="utf-8")

    proc = subprocess.Popen(
        [
            "kubectl",
            "port-forward",
            "-n",
            namespace,
            service,
            f"{local_port}:{remote_port}",
        ],
        stdout=f,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )

    for _ in range(ready_timeout_s):
        time.sleep(1)

        if proc.poll() is not None:
            f.close()
            print("Port-forward ist fehlgeschlagen:", flush=True)
            print(log_path.read_text(encoding="utf-8", errors="replace"), flush=True)
            return None, None

        content = log_path.read_text(encoding="utf-8", errors="replace")
        if "Forwarding from" in content:
            print("Port-forward bereit.", flush=True)
            return proc, f

    print("Port-forward Timeout:", flush=True)
    print(log_path.read_text(encoding="utf-8", errors="replace"), flush=True)

    try:
        proc.terminate()
    except ProcessLookupError:
        pass

    f.close()
    return None, None


def stop_port_forward(proc, file_handle):
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    if file_handle is not None:
        try:
            file_handle.close()
        except Exception:
            pass



def copy_final_model_from_serverapp(
    namespace,
    run_dir,
    log_file,
    env,
    pod_selector="app=flower-superlink",
    container="superexec-serverapp",
    source_path="/tmp/flower-final-model/final_model.pt",
):
    """Copy final model from the Flower ServerApp container into the run directory."""
    run_dir = Path(run_dir)
    log_file = Path(log_file)
    model_dir = run_dir / "model"
    model_dir.mkdir(parents=True, exist_ok=True)

    target_path = model_dir / "final_model.pt"

    print("---", flush=True)
    print("Kopiere finales Modell aus dem Flower-ServerApp-Container...", flush=True)

    get_pod_cmd = [
        "kubectl",
        "get",
        "pod",
        "-n",
        namespace,
        "-l",
        pod_selector,
        "-o",
        "jsonpath={.items[0].metadata.name}",
    ]

    with log_file.open("w", encoding="utf-8") as f:
        f.write("$ " + " ".join(get_pod_cmd) + "\n")
        f.flush()

        pod_result = subprocess.run(
            get_pod_cmd,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        f.write(pod_result.stdout)
        f.write(pod_result.stderr)
        f.flush()

    if pod_result.returncode != 0:
        print("Konnte Flower-SuperLink-Pod nicht ermitteln.", flush=True)
        return pod_result.returncode, None

    pod_name = pod_result.stdout.strip()

    if not pod_name:
        print("Kein Flower-SuperLink-Pod gefunden.", flush=True)
        return 1, None

    copy_rc = run_command(
        [
            "kubectl",
            "cp",
            "-n",
            namespace,
            "-c",
            container,
            f"{pod_name}:{source_path}",
            str(target_path),
        ],
        log_file=log_file,
        env=env,
        append=True,
    )

    if copy_rc == 0 and target_path.exists():
        print(f"Finales Modell gespeichert: {target_path}", flush=True)
        return 0, str(target_path)

    print("Finales Modell konnte nicht kopiert werden.", flush=True)
    return copy_rc or 1, None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Pfad zur Experiment-YAML.")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_yaml(config_path)

    experiment = config.get("experiment", {})
    project = config.get("project", {})
    energy = config.get("energy", {})
    metrics_export = config.get("metrics_export", {})
    report = config.get("report", {})

    cluster_profile_path = config.get("cluster_profile")
    if not cluster_profile_path:
        print("Fehler: cluster_profile fehlt in der Experiment-Konfiguration.")
        sys.exit(1)

    cluster_profile = load_yaml(cluster_profile_path)
    k8s_cfg = cluster_profile.get("kubernetes", {})

    experiment_name = experiment.get("name", "experiment")
    strategy = experiment.get("strategy", "unknown")
    dataset = experiment.get("dataset", "unknown")
    model = experiment.get("model", project.get("app_name", "unknown"))

    run_config = project.get("run_config", {})
    kubernetes_runtime = config.get("kubernetes_runtime", {})
    client_groups = get_client_groups(kubernetes_runtime)

    if client_groups:
        total_replicas = sum(int(group.get("replicas", 1)) for group in client_groups)
        configured_partitions = run_config.get("num-partitions")

        if configured_partitions is None:
            run_config["num-partitions"] = total_replicas
        elif int(configured_partitions) != total_replicas:
            print(
                "Fehler: project.run_config.num-partitions muss der Summe "
                f"der kubernetes_runtime.client_groups replicas entsprechen. "
                f"num-partitions={configured_partitions}, replicas_sum={total_replicas}",
                flush=True,
            )
            sys.exit(1)

    requested_rounds = run_config.get("num-server-rounds")

    run_id = f"{safe_timestamp()}_{experiment_name}"
    k8s_run_id = k8s_safe_name(run_id)

    run_dir = Path("runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Pass absolute run directory to the Flower ServerApp
    run_config["run-dir"] = str(run_dir.resolve())

    namespace = k8s_cfg.get("namespace", "flwr")
    label_selector_template = k8s_cfg.get("label_selector", "run_id={K8S_RUN_ID}")
    label_selector = (
        label_selector_template
        .replace("{RUN_ID}", run_id)
        .replace("{K8S_RUN_ID}", k8s_run_id)
    )

    node_mapping_path = energy.get("node_mapping", "node_mapping.json")
    node_mapping = load_json(node_mapping_path, default={}) or {}
    include_nodes = cluster_profile.get("nodes", {}).get("include_for_energy", [])

    env = os.environ.copy()
    env["RUN_ID"] = run_id
    env["K8S_RUN_ID"] = k8s_run_id
    env["RUN_DIR"] = str(run_dir)
    env["TZ"] = "Europe/Berlin"

    env["STRATEGY"] = strategy
    env["DATASET"] = dataset
    env["MODEL_NAME"] = model
    env["NUM_ROUNDS"] = str(requested_rounds or "")
    env["LOCAL_EPOCHS"] = str(run_config.get("local-epochs", ""))
    env["LEARNING_RATE"] = str(run_config.get("learning-rate", ""))
    env["BATCH_SIZE"] = str(run_config.get("batch-size", ""))

    env["APP_IMAGE"] = str(
        kubernetes_runtime.get("app_image", "thadegreen/quickstart-pytorch-flex:0.1")
    )
    env["SUPERLINK_IMAGE"] = str(
        kubernetes_runtime.get("superlink_image", "flwr/superlink:1.29.0")
    )
    env["SUPERNODE_IMAGE"] = str(
        kubernetes_runtime.get("supernode_image", "flwr/supernode:1.29.0")
    )
    env["CLIENT_WORKLOADS"] = generate_client_workloads(
        kubernetes_runtime=kubernetes_runtime,
        run_config=run_config,
        namespace=namespace,
        env=env,
    )

    metadata = {
        "schema_version": 2,
        "run_id": run_id,
        "k8s_run_id": k8s_run_id,
        "experiment_config": str(config_path),
        "cluster_profile": str(cluster_profile_path),
        "experiment": experiment,
        "project": {
            "app_dir": project.get("app_dir"),
            "app_name": project.get("app_name"),
            "superlink_connection": project.get("superlink_connection"),
            "stream": project.get("stream", True),
            "run_config": run_config,
            "metadata": project.get("metadata", {}),
        },
        "strategy": strategy,
        "dataset": dataset,
        "model": model,
        "requested_num_server_rounds": requested_rounds,
        "namespace": namespace,
        "label_selector": label_selector,
        "included_nodes": include_nodes,
        "start_time": utc_now_iso(),
        "start_time_local": local_now_iso(),
        "end_time": None,
        "end_time_local": None,
        "status": "running",
        "valid": None,
        "return_code": None,
        "nodes": [],
        "pods": [],
    }

    write_json(run_dir / "run_metadata.json", metadata)
    write_yaml(run_dir / "experiment_config.yaml", config)
    write_yaml(run_dir / "cluster_profile.yaml", cluster_profile)

    orchestration_log = run_dir / "orchestration.log"
    flower_log = run_dir / "flower.log"
    portforward_log = run_dir / "portforward.log"

    print(f"Starte Experiment: {run_id}", flush=True)
    print(f"Kubernetes Run ID: {k8s_run_id}", flush=True)
    print(f"Run-Verzeichnis: {run_dir}", flush=True)
    print(f"Requested server rounds: {requested_rounds}", flush=True)
    print(f"Run config: {build_run_config_string(run_config)}", flush=True)

    return_code = 1
    pf_proc = None
    pf_file = None

    try:
        if k8s_cfg.get("cleanup_before_run", True):
            cleanup_kubernetes(
                namespace=namespace,
                cleanup_cfg=k8s_cfg.get("cleanup", {}),
                log_file=orchestration_log,
                env=env,
            )

            cleanup_dynamic_client_workloads(
                namespace=namespace,
                log_file=orchestration_log,
                env=env,
            )

        template_path = k8s_cfg.get("template")
        rendered_manifest = run_dir / "k8s-rendered.yaml"

        render_template(
            template_path=template_path,
            output_path=rendered_manifest,
            env=env,
        )

        rc = run_command(
            ["kubectl", "apply", "-f", str(rendered_manifest)],
            log_file=orchestration_log,
            env=env,
        )
        if rc != 0:
            return_code = rc
            raise RuntimeError("kubectl apply fehlgeschlagen")

        rc = rollout_kubernetes(
            namespace=namespace,
            rollouts=build_rollouts(k8s_cfg.get("rollouts", []), kubernetes_runtime),
            log_file=orchestration_log,
            env=env,
        )
        if rc != 0:
            return_code = rc
            raise RuntimeError("Kubernetes rollout fehlgeschlagen")

        pf_cfg = k8s_cfg.get("port_forward", {})
        pf_proc, pf_file = start_port_forward(
            namespace=namespace,
            service=pf_cfg.get("service", "svc/flower-superlink"),
            local_port=pf_cfg.get("local_port", 9093),
            remote_port=pf_cfg.get("remote_port", 9093),
            log_path=portforward_log,
            env=env,
            ready_timeout_s=pf_cfg.get("ready_timeout_s", 30),
        )

        if pf_proc is None:
            return_code = 1
            raise RuntimeError("Port-forward konnte nicht gestartet werden")

        app_dir = Path(project["app_dir"])
        stream = bool(project.get("stream", True))
        connection = project.get("superlink_connection")
        run_config_string = build_run_config_string(run_config)

        flower_cmd = ["flwr", "run", "."]

        # Standardmäßig nutzt Flower die Default-Verbindung aus ~/.flwr/config.toml.
        # Wenn superlink_connection gesetzt ist, wird sie als positional argument ergänzt.
        if connection:
            flower_cmd.append(str(connection))

        if stream:
            flower_cmd.append("--stream")

        if run_config_string:
            flower_cmd.extend(["--run-config", run_config_string])

        flower_env = env.copy()
        flower_env["PYTHONUNBUFFERED"] = "1"
        flower_env["NO_COLOR"] = "1"

        return_code = run_command_pty(
            flower_cmd,
            log_file=flower_log,
            cwd=app_dir,
            env=flower_env,
            append=False,
        )

    except Exception as exc:
        print(f"Fehler: {exc}", flush=True)

    finally:
        stop_port_forward(pf_proc, pf_file)

        metadata["end_time"] = utc_now_iso()
        metadata["end_time_local"] = local_now_iso()
        metadata["return_code"] = return_code
        metadata["status"] = "completed" if return_code == 0 else "failed"

        pods = collect_pods(namespace, label_selector)
        nodes = collect_nodes_from_pods(pods, node_mapping)
        nodes = add_included_nodes(nodes, include_nodes, node_mapping)

        metadata["pods"] = pods
        metadata["nodes"] = nodes

        write_json(run_dir / "run_metadata.json", metadata)


    model_copy_rc = None
    final_model_path = None

    if return_code == 0:
        model_copy_log = run_dir / "model_copy.log"
        model_copy_rc, final_model_path = copy_final_model_from_serverapp(
            namespace=namespace,
            run_dir=run_dir,
            log_file=model_copy_log,
            env=env,
        )

        metadata = load_json(run_dir / "run_metadata.json", default=metadata) or metadata
        metadata["model_copy_return_code"] = model_copy_rc
        metadata["final_model_path"] = final_model_path
        metadata["model_copy_log"] = str(model_copy_log)
        write_json(run_dir / "run_metadata.json", metadata)
    else:
        print("---", flush=True)
        print("Modellkopie übersprungen, weil der Flower-Lauf fehlgeschlagen ist.", flush=True)

    parser_log = run_dir / "parser.log"
    parse_rc = run_command(
        [sys.executable, "scripts/parse_flower_log.py", str(run_dir)],
        log_file=parser_log,
        env=env,
    )

    augment_rc = run_command(
        [sys.executable, "scripts/augment_round_metrics_from_flower_log.py", str(run_dir)],
        log_file=parser_log,
        env=env,
        append=True,
    )

    validation = load_json(run_dir / "validation.json", default={}) or {}
    valid = bool(validation.get("valid", False))

    metadata = load_json(run_dir / "run_metadata.json", default=metadata)
    metadata["valid"] = valid
    metadata["validation"] = validation
    metadata["parser_return_code"] = parse_rc
    metadata["round_metrics_augment_return_code"] = augment_rc
    write_json(run_dir / "run_metadata.json", metadata)

    if metrics_export.get("enabled", False):
        run_on_invalid = bool(metrics_export.get("run_on_invalid", False))

        if valid or run_on_invalid:
            metrics_export_log = run_dir / "metrics_export.log"
            metrics_export_script = metrics_export.get(
                "script",
                "scripts/write_fl_metrics_to_influx.py",
            )

            print("---", flush=True)
            print("Exportiere FL-Metriken nach InfluxDB...", flush=True)

            metrics_export_rc = run_command(
                [sys.executable, metrics_export_script, str(run_dir)],
                log_file=metrics_export_log,
                env=env,
                append=False,
            )

            extra_metrics_export_rc = run_command(
                [sys.executable, "scripts/write_extra_round_metrics_to_influx.py", str(run_dir)],
                log_file=metrics_export_log,
                env=env,
                append=True,
            )

            metadata = load_json(run_dir / "run_metadata.json", default=metadata)
            metadata["metrics_export_return_code"] = metrics_export_rc
            metadata["extra_round_metrics_export_return_code"] = extra_metrics_export_rc
            metadata["metrics_export_log"] = str(metrics_export_log)
            write_json(run_dir / "run_metadata.json", metadata)

        else:
            print("---", flush=True)
            print("FL-Metrikexport übersprungen, weil der Flower-Lauf nicht valide ist.", flush=True)

    if energy.get("enabled", False):
        run_on_invalid = bool(energy.get("run_on_invalid", False))

        if valid or run_on_invalid:
            energy_log = run_dir / "energy_summary.log"
            energy_script = energy.get("script", "scripts/energy_summary.py")

            print("---", flush=True)
            print("Starte Energiezusammenfassung...", flush=True)

            energy_rc = run_command(
                [sys.executable, energy_script, str(run_dir)],
                log_file=energy_log,
                env=env,
                append=False,
            )

            metadata = load_json(run_dir / "run_metadata.json", default=metadata)
            metadata["energy_summary_return_code"] = energy_rc
            metadata["energy_summary_log"] = str(energy_log)
            write_json(run_dir / "run_metadata.json", metadata)

        else:
            print("---", flush=True)
            print("Energiezusammenfassung übersprungen, weil der Flower-Lauf nicht valide ist.", flush=True)

    if report.get("enabled", True):
        report_log = run_dir / "report_generation.log"
        run_command(
            [sys.executable, "scripts/generate_report.py", str(run_dir)],
            log_file=report_log,
            env=env,
        )

    final_meta = load_json(run_dir / "run_metadata.json", default={}) or {}

    print("---", flush=True)
    print(f"Experiment beendet: {final_meta.get('status')}", flush=True)
    print(f"Valid: {final_meta.get('valid')}", flush=True)
    print(f"Return Code: {return_code}", flush=True)
    print(f"Run-Verzeichnis: {run_dir}", flush=True)

    sys.exit(return_code)


if __name__ == "__main__":
    main()
