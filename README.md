# GreenAI Orchestrator

Kurze Anleitung für den Orchestrator der GreenAI-Demonstrationsumgebung.

Dieses Repository startet und dokumentiert Flower-Experimente auf einem K3s/Kubernetes-Cluster. Es verteilt Flower-Komponenten auf die vorhandenen Nodes, schreibt Trainings-, Energie- und Inferenzmetriken nach InfluxDB und speichert alle Laufdaten in `runs/`.

Die Nachhaltigkeitsarchitektur mit Mosquitto, Node-RED, InfluxDB und Grafana liegt separat in `green-ai-sustainability`.

## Was macht der Orchestrator?

1. Experiment aus einer YAML-Datei lesen.
2. Kubernetes-Manifeste aus Templates erzeugen.
3. Flower SuperLink und SuperNodes im K3s-Cluster starten.
4. Training ausführen und Logs/Metriken speichern.
5. Energiedaten aus InfluxDB auswerten.
6. Finales Modell, Report und Metriken unter `runs/<run_id>/` ablegen.

## Wichtige Dateien

```text
run_experiment.py                         # startet und dokumentiert Trainingsläufe
cluster_profiles/k3s_greenai.yaml         # Clusterprofil für die K3s-Umgebung
node_mapping.json                         # Zuordnung zwischen Experiment-Nodes und realen Geräten
experiments/                              # YAML-Dateien für Trainingskonfigurationen
k8s/                                      # Kubernetes-Templates
quickstart-pytorch/                       # Flower-App mit Modell, ServerApp und ClientApp
scripts/                                  # Auswertungs-, Influx- und Inferenzskripte
Dockerfile.greenai                        # Dockerfile für das GreenAI-Image
.env.influx.example                       # Vorlage für die InfluxDB-Verbindung
```

## Voraussetzungen

- Linux-Umgebung mit Python 3
- Zugriff auf einen laufenden K3s/Kubernetes-Cluster
- funktionierendes `kubectl`
- Docker, falls Images gebaut werden sollen
- laufende InfluxDB aus der Nachhaltigkeitsarchitektur
- passende Nodes und Labels gemäß `cluster_profiles/k3s_greenai.yaml` und `node_mapping.json`

Cluster prüfen:

```bash
kubectl get nodes
kubectl get namespace flwr || kubectl create namespace flwr
```

## Installation

```bash
git clone https://gitup.uni-potsdam.de/egenhoff/green-ai-orchestrator.git
cd green-ai-orchestrator

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e quickstart-pytorch
pip install influxdb-client pyyaml requests
```

Für lokale oder HTTP-basierte Inferenztests zusätzlich:

```bash
pip install fastapi uvicorn python-multipart pillow httpx
```

## InfluxDB konfigurieren

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
```

Laden und testen:

```bash
set -a
source .env.influx
set +a
curl "$INFLUX_URL/health"
```

Die echte `.env.influx` enthält Zugangsdaten und darf nicht in Git versioniert werden.

## Experiment anpassen

Experimentkonfigurationen liegen in `experiments/`. Eine Konfiguration beschreibt unter anderem:

- Strategie, z. B. `FedAvg` oder `FedAdam`
- Datensatz und Datensplit
- Anzahl der Runden
- lokale Epochen
- Lernrate und Batch Size
- Client-Gruppen und Kubernetes-Platzierung

Für einen neuen Lauf am einfachsten eine bestehende YAML-Datei kopieren und anpassen.

## Experiment starten

```bash
source .venv/bin/activate
set -a
source .env.influx
set +a

python run_experiment.py \
  --config experiments/quickstart_fedavg_cifar10_iid_10r_flex.yaml \
  --profile cluster_profiles/k3s_greenai.yaml
```

Weitere Optionen:

```bash
python run_experiment.py --help
```

## Ergebnisse

Jeder Lauf erzeugt einen eigenen Ordner:

```text
runs/<run_id>/
```

Dort liegen typischerweise:

```text
Konfigurationen
Kubernetes-Manifest
Flower-Logs
Validierungsergebnisse
Metriken
Energieauswertung
Report als Markdown/HTML
model/final_model.pt
run_metadata.json
```

Die wichtigsten Metriken werden zusätzlich nach InfluxDB geschrieben und können in Grafana visualisiert werden.

## Inferenz testen

Ein gespeichertes Modell kann anschließend auf einem K3s-Node als Inferenzdienst getestet werden. Dafür ist vor allem dieses Skript relevant:

```bash
scripts/run_inference_eval_k3s.sh
```

Der Inferenzlauf nutzt ein Modell aus `runs/<run_id>/model/final_model.pt`, erzeugt Kubernetes-Ressourcen für den Inferenzdienst und schreibt die Ergebnisse wieder nach InfluxDB.

## Image bauen

Falls Anwendungscode oder Abhängigkeiten verändert wurden, muss ein neues Image gebaut werden:

```bash
docker build -t greenai-orchestrator:local -f Dockerfile.greenai .
```

Für gemischte x86-/ARM-Cluster sollte das Image als Multi-Arch-Image gebaut und in eine Registry gepusht werden.

## Nicht committen

```text
.env.influx
runs/
data/
.venv/
*.log
Tokens
Passwörter
API-Keys
private Schlüssel
```

Vor einem Commit prüfen:

```bash
git status
git diff --cached --name-only
git grep -n -i "token\|password\|secret\|api_key\|apikey\|passwd"
```

## Kurzablauf

```bash
kubectl get nodes
source .venv/bin/activate
set -a && source .env.influx && set +a
python run_experiment.py --config <experiment-yaml> --profile cluster_profiles/k3s_greenai.yaml
```

Danach Ergebnisse in `runs/<run_id>/` und in Grafana prüfen.
