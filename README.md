# GreenAI Orchestrator

Dieses Repository enthält den GreenAI-Orchestrator der Demonstrationsumgebung. Der Orchestrator steuert föderierte Lern- und Inferenzexperimente auf einem k3s-/Kubernetes-Cluster, verwaltet Experimentkonfigurationen, startet Flower-Komponenten und schreibt Energie-, CO₂- und Experimentmetriken in eine externe InfluxDB.

Die Nachhaltigkeitsarchitektur selbst liegt in einem separaten Repository:

```text
green-ai-sustainability
```

Dieses Repository enthält den experimentellen Orchestrator, also den Teil, der auf der GreenAI-VM betrieben wird.

## Zweck

Der GreenAI-Orchestrator dient dazu,

- föderierte Lernexperimente zu konfigurieren,
- Flower-Komponenten auf k3s zu starten,
- Clients bestimmten physischen Nodes zuzuordnen,
- Trainings-, Runden- und Inferenzmetriken auszuwerten,
- Energie- und CO₂-Messwerte aus der externen Nachhaltigkeitsarchitektur zu referenzieren,
- Ergebnisse in InfluxDB zu schreiben,
- Berichte und Zusammenfassungen aus Experimentläufen zu erzeugen.

Die Monitoring- und Datenhaltungsschicht läuft außerhalb dieser VM in der separaten Nachhaltigkeitsarchitektur.

## Architekturüberblick

Die GreenAI-Gesamtumgebung besteht aus zwei Hauptteilen:

```text
1. GreenAI Orchestrator
   - läuft in der GreenAI-VM
   - enthält Experimentlogik, Flower-Code, k3s-Templates und Auswertungsskripte
   - schreibt Metriken in die externe InfluxDB

2. GreenAI Sustainability Architecture
   - läuft auf dem Windows-/Docker-System
   - stellt Mosquitto, InfluxDB, Node-RED und Grafana bereit
   - sammelt Energie- und CO₂-Daten
```

Die Verbindung zwischen beiden Teilen erfolgt über InfluxDB.

Innerhalb der Nachhaltigkeitsarchitektur kann InfluxDB über den Docker-Servicenamen erreicht werden:

```text
http://influxdb:8086
```

Aus der GreenAI-Orchestrator-VM muss stattdessen die Host-IP oder der Hostname des Systems verwendet werden, auf dem die Nachhaltigkeitsarchitektur läuft:

```text
http://<SUSTAINABILITY-HOST-IP>:8086
```

## Repository-Inhalt

```text
green-ai-orchestrator/
├── Dockerfile.greenai
├── .dockerignore
├── .env.influx.example
├── .gitignore
├── cluster_profiles/
│   └── k3s_greenai.yaml
├── experiments/
├── k8s/
├── node_mapping.json
├── quickstart-pytorch/
├── run_experiment.py
└── scripts/
```

## Nicht im Repository enthalten

Folgende Dateien und Ordner sind absichtlich nicht versioniert:

```text
.env.influx
runs/
data/
.venv/
__pycache__/
*.log
*.bak
lokale Datensätze
lokale Experimentartefakte
echte Tokens
Passwörter
API-Keys
```

Das Repository ist öffentlich und enthält daher nur reproduzierbare Konfigurationen, aber keine Zugangsdaten oder großen Laufzeitdaten.

## Voraussetzungen

Benötigt werden:

- Linux-VM für den GreenAI-Orchestrator
- Python 3
- Docker
- Zugriff auf den k3s-/Kubernetes-Cluster
- funktionierendes `kubectl`
- Zugriff auf die externe InfluxDB der Nachhaltigkeitsarchitektur
- Git

Prüfen:

```bash
python3 --version
git --version
docker --version
kubectl version --client
```

Cluster prüfen:

```bash
kubectl get nodes
kubectl get namespaces
```

Der Standard-Namespace für die Flower-Experimente ist:

```text
flwr
```

Falls der Namespace noch nicht existiert:

```bash
kubectl create namespace flwr
```

## Installation

Repository klonen:

```bash
git clone https://gitup.uni-potsdam.de/egenhoff/green-ai-orchestrator.git
cd green-ai-orchestrator
```

Falls das Repository noch nicht umbenannt wurde, lautet die bisherige Adresse:

```bash
git clone https://gitup.uni-potsdam.de/egenhoff/green-ai-architecture.git
cd green-ai-architecture
```

Virtuelle Python-Umgebung erstellen:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
```

Flower-Beispielpaket installieren:

```bash
pip install -e quickstart-pytorch
```

Zusätzliche Abhängigkeiten installieren:

```bash
pip install influxdb-client pyyaml requests
```

Falls Inferenzdienste lokal genutzt werden:

```bash
pip install fastapi uvicorn python-multipart pillow httpx
```

## InfluxDB-Konfiguration

Die Datei `.env.influx.example` enthält Platzhalter für die Verbindung zur externen InfluxDB.

Kopieren:

```bash
cp .env.influx.example .env.influx
nano .env.influx
```

Beispiel:

```bash
export INFLUX_URL="http://<SUSTAINABILITY-HOST-IP>:8086"
export INFLUX_ORG="thade"
export INFLUX_BUCKET="greenaidata"
export INFLUX_TOKEN="PLEASE_SET_TOKEN_HERE"

export POWER_MEASUREMENT="shelly_plug"
export POWER_FIELD="power_w"
export POWER_NODE_TAG="device"
export POWER_GENERATION_TAG="generation"
export POWER_GENERATION_VALUE="gen2_or_gen3"
```

Die echte Datei `.env.influx` darf nicht in Git gespeichert werden.

Influx-Verbindung testen:

```bash
set -a
source .env.influx
set +a

curl "$INFLUX_URL/health"
```

## Clusterprofil

Das zentrale Clusterprofil liegt unter:

```text
cluster_profiles/k3s_greenai.yaml
```

Es beschreibt unter anderem:

- Kubernetes-Namespace,
- zu verwendendes k8s-Template,
- Cleanup-Regeln vor Experimentläufen,
- Rollout-Timeouts,
- Port-Forwarding zum Flower SuperLink,
- beteiligte Nodes,
- Client-Zuordnung zu physischen Geräten.

Typische Nodes der Demonstrationsumgebung:

```text
greenai-control
cloud-gpu
old-laptop
pi5-16gb
pi5-8gb
```

Die tatsächliche Zuordnung kann in `cluster_profiles/k3s_greenai.yaml` und `node_mapping.json` angepasst werden.

## Kubernetes-Templates

Die Kubernetes-Templates liegen unter:

```text
k8s/
```

Wichtige Dateien:

```text
k8s/flower-experiment.yaml.tpl
k8s/flower-experiment.static-4clients.yaml.tpl
```

Diese Templates werden für die Bereitstellung der Flower-Komponenten im k3s-Cluster verwendet.

## Experimentkonfigurationen

Die Experimentkonfigurationen liegen unter:

```text
experiments/
```

Beispiele:

```text
quickstart_fedavg_cifar10_10r.yaml
quickstart_fedavg_cifar10_iid_10r_flex.yaml
quickstart_fedavg_cifar10_iid_20r_ep2_b64.yaml
quickstart_fedadam_cifar10_weighted_iid_10r.yaml
quickstart_fedadam_cifar10_weighted_iid_20r_b64.yaml
quickstart_fedadam_cifar10_weighted_iid_30r.yaml
```

Die Konfigurationen beschreiben u. a. Strategie, Datensatz, Rundenanzahl, Batch-Größe und Client-Verteilung.

## Experiment starten

Vor dem Start:

```bash
source .venv/bin/activate
set -a
source .env.influx
set +a
kubectl get nodes
```

Hilfe bzw. verfügbare Optionen prüfen:

```bash
python run_experiment.py --help
```

Typischer Experimentstart:

```bash
python run_experiment.py \
  --config experiments/quickstart_fedavg_cifar10_iid_10r_flex.yaml \
  --profile cluster_profiles/k3s_greenai.yaml
```

Falls `run_experiment.py` andere Parameter erwartet, sollte die Ausgabe von `python run_experiment.py --help` verwendet werden.

## Inferenzdienste

Für lokale oder HTTP-basierte Inferenztests sind folgende Dateien relevant:

```text
scripts/inference_service_local.py
scripts/infer_once_local.py
scripts/benchmark_inference_http_local.py
scripts/run_inference_eval_k3s.sh
```

Das Dockerfile für ein erweitertes GreenAI-Image liegt unter:

```text
Dockerfile.greenai
```

Image lokal bauen:

```bash
docker build -t greenai-orchestrator:local -f Dockerfile.greenai .
```

## Metriken und Auswertung

Die Skripte unter `scripts/` unterstützen u. a.:

- Parsen von Flower-Logs,
- Schreiben von FL-Metriken nach InfluxDB,
- Ergänzen zusätzlicher Rundenmetriken,
- Auswertung von Energie- und CO₂-Metriken,
- Inferenz-Benchmarking,
- Report-Erzeugung.

Wichtige Skripte:

```text
scripts/write_fl_metrics_to_influx.py
scripts/write_extra_round_metrics_to_influx.py
scripts/write_inference_metrics_to_influx.py
scripts/energy_summary.py
scripts/energy_summary_inference.py
scripts/generate_report.py
scripts/parse_flower_log.py
```

## Ergebnisse und Laufdaten

Experimentläufe, lokale Datensätze und generierte Artefakte gehören nicht ins Git-Repository.

Typische lokale Ordner:

```text
runs/
data/
```

Diese Ordner sollten separat gesichert werden, z. B. als lokales Archiv oder in einem geschützten Uni-Speicher.

Beispiel:

```bash
cd ~

tar -czf greenai-orchestrator-local-data-$(date +%F).tar.gz \
  greenai-experiments/runs \
  greenai-experiments/data \
  greenai-experiments/.env.influx
```

Achtung: Dieses Archiv enthält mit `.env.influx` sensible Zugangsdaten und darf nicht öffentlich abgelegt werden.

## Beziehung zur Nachhaltigkeitsarchitektur

Die Nachhaltigkeitsarchitektur stellt folgende Dienste bereit:

```text
Mosquitto: localhost:1883
Node-RED:  localhost:1880
Grafana:   localhost:3000
InfluxDB:  localhost:8086
```

Aus Sicht dieser VM ist vor allem InfluxDB relevant.

Die InfluxDB-Verbindung wird über `.env.influx` konfiguriert:

```bash
export INFLUX_URL="http://<SUSTAINABILITY-HOST-IP>:8086"
export INFLUX_ORG="thade"
export INFLUX_BUCKET="greenaidata"
export INFLUX_TOKEN="PLEASE_SET_TOKEN_HERE"
```

Die Nachhaltigkeitsarchitektur selbst liegt nicht in diesem Repository, sondern im separaten Repository:

```text
green-ai-sustainability
```

## Sicherheitshinweise

Dieses Repository ist öffentlich. Deshalb dürfen keine echten Zugangsdaten committed werden.

Nicht committen:

```text
.env.influx
InfluxDB-Tokens
API-Keys
Passwörter
private Schlüssel
runs/
data/
.venv/
Backup-Archive
```

Vor jedem Commit prüfen:

```bash
git status
git diff --cached --name-only
git grep -n -i "token\|password\|secret\|api_key\|apikey\|passwd"
```

Erwartete Treffer sind nur Platzhalter, z. B.:

```text
PLEASE_SET_TOKEN_HERE
```

Wenn ein echter Token versehentlich veröffentlicht wurde, sollte er als kompromittiert betrachtet und erneuert werden.

## Typische Git-Befehle

Status prüfen:

```bash
git status
```

Änderungen hinzufügen:

```bash
git add README.md
```

Commit erstellen:

```bash
git commit -m "Update documentation"
```

Push nach GitUp:

```bash
git push
```

## Reproduzierbarkeit

Für eine reproduzierbare Ausführung werden benötigt:

1. Dieses Repository
2. Zugriff auf den k3s-/Kubernetes-Cluster
3. Eine lokale `.env.influx` auf Basis von `.env.influx.example`
4. Die externe Nachhaltigkeitsarchitektur mit InfluxDB
5. Optional: separat gesicherte `runs/` und `data/`
6. Optional: lokal gebaute oder veröffentlichte Container-Images

Minimaler Wiederherstellungsablauf:

```bash
git clone https://gitup.uni-potsdam.de/egenhoff/green-ai-orchestrator.git
cd green-ai-orchestrator

cp .env.influx.example .env.influx
nano .env.influx

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e quickstart-pytorch
pip install influxdb-client pyyaml requests

kubectl get nodes
set -a
source .env.influx
set +a
curl "$INFLUX_URL/health"
```

Danach können Experimente über `run_experiment.py` und die YAML-Dateien in `experiments/` gestartet werden.

## Hinweis zur Masterarbeit

Dieses Repository dokumentiert den Orchestrierungs- und Experimentteil der GreenAI-Demonstrationsumgebung. Die Nachhaltigkeitsarchitektur mit Mosquitto, Node-RED, InfluxDB und Grafana wird separat betrieben und separat versioniert. Die Trennung ermöglicht eine klare Zuordnung zwischen Experimentsteuerung und Monitoring-/Datenhaltungsschicht.
