#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Nutzung:"
  echo "  $0 <TRAIN_RUN_ID> [EVAL_ID]"
  exit 1
fi

TRAIN_RUN="$1"
EVAL_ID="${2:-k3s_old-laptop_c4_1k}"

NAMESPACE="${NAMESPACE:-flwr}"
IMAGE="${IMAGE:-thadegreen/quickstart-pytorch-greenai:0.2}"
IMAGE_PULL_POLICY="${IMAGE_PULL_POLICY:-Always}"

INFERENCE_NODE="${INFERENCE_NODE:-old-laptop}"
BENCHMARK_NODE="${BENCHMARK_NODE:-old-laptop}"

SAMPLES="${SAMPLES:-1000}"
WARMUP="${WARMUP:-50}"
HEALTH_PINGS="${HEALTH_PINGS:-10}"
CONCURRENCY="${CONCURRENCY:-4}"
TIMEOUT="${TIMEOUT:-10.0}"

# Der Benchmark-Pod schläft nach dem Benchmark noch kurz,
# damit kubectl cp die Dateien kopieren kann.
RESULT_HOLD_SECONDS="${RESULT_HOLD_SECONDS:-300}"

EXPORT_INFLUX="${EXPORT_INFLUX:-1}"
RUN_ENERGY_SUMMARY="${RUN_ENERGY_SUMMARY:-1}"
CLEANUP="${CLEANUP:-0}"

TRAIN_RUN_DIR="runs/${TRAIN_RUN}"
MODEL_PATH="${TRAIN_RUN_DIR}/model/final_model.pt"
OUT_DIR="${TRAIN_RUN_DIR}/inference/${EVAL_ID}"

if [ ! -f "$MODEL_PATH" ]; then
  echo "Fehler: Modell nicht gefunden: $MODEL_PATH"
  exit 1
fi

mkdir -p "$OUT_DIR"

K8S_EVAL_ID="$(echo "$EVAL_ID" \
  | tr '[:upper:]' '[:lower:]' \
  | sed -E 's/[^a-z0-9-]+/-/g; s/^-+//; s/-+$//' \
  | cut -c1-45)"

if [ -z "$K8S_EVAL_ID" ]; then
  echo "Fehler: K8S_EVAL_ID ist leer."
  exit 1
fi

CONFIGMAP_NAME="infer-model-${K8S_EVAL_ID}"
DEPLOYMENT_NAME="infer-${K8S_EVAL_ID}"
SERVICE_NAME="infer-${K8S_EVAL_ID}"
JOB_NAME="bench-${K8S_EVAL_ID}"
MANIFEST_PATH="${OUT_DIR}/k8s-inference-rendered.yaml"

MODEL_SIZE_BYTES="$(stat -c%s "$MODEL_PATH")"
if [ "$MODEL_SIZE_BYTES" -gt 900000 ]; then
  echo "Warnung: final_model.pt ist größer als 900 KB."
  echo "ConfigMap kann problematisch werden. Für größere Modelle später PVC/NFS nutzen."
fi

echo "---"
echo "Train Run:       $TRAIN_RUN"
echo "Eval ID:         $EVAL_ID"
echo "Namespace:       $NAMESPACE"
echo "Image:           $IMAGE"
echo "Inference Node:  $INFERENCE_NODE"
echo "Benchmark Node:  $BENCHMARK_NODE"
echo "Samples:         $SAMPLES"
echo "Concurrency:     $CONCURRENCY"
echo "Output Dir:      $OUT_DIR"
echo "---"

echo "Lösche alte Objekte mit gleicher Eval-ID..."
kubectl delete job "$JOB_NAME" -n "$NAMESPACE" --ignore-not-found=true
kubectl delete deployment "$DEPLOYMENT_NAME" -n "$NAMESPACE" --ignore-not-found=true
kubectl delete service "$SERVICE_NAME" -n "$NAMESPACE" --ignore-not-found=true
kubectl delete configmap "$CONFIGMAP_NAME" -n "$NAMESPACE" --ignore-not-found=true

echo "Erzeuge ConfigMap mit final_model.pt..."
kubectl create configmap "$CONFIGMAP_NAME" \
  -n "$NAMESPACE" \
  --from-file=final_model.pt="$MODEL_PATH"

cat > "$MANIFEST_PATH" <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${DEPLOYMENT_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: greenai-inference
    eval_id_k8s: ${K8S_EVAL_ID}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: greenai-inference
      eval_id_k8s: ${K8S_EVAL_ID}
  template:
    metadata:
      labels:
        app: greenai-inference
        eval_id_k8s: ${K8S_EVAL_ID}
    spec:
      nodeSelector:
        kubernetes.io/hostname: ${INFERENCE_NODE}
      containers:
        - name: inference
          image: ${IMAGE}
          imagePullPolicy: ${IMAGE_PULL_POLICY}
          command: ["python3", "/app/scripts/inference_service_local.py"]
          args:
            - "--model=/models/final_model.pt"
            - "--host=0.0.0.0"
            - "--port=8080"
          env:
            - name: NODE_NAME
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: POD_NAMESPACE
              valueFrom:
                fieldRef:
                  fieldPath: metadata.namespace
            - name: EVAL_ID
              value: "${EVAL_ID}"
            - name: TRAIN_RUN_ID
              value: "${TRAIN_RUN}"
          ports:
            - name: http
              containerPort: 8080
          readinessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 12
          resources:
            requests:
              cpu: "300m"
              memory: "512Mi"
              ephemeral-storage: "1Gi"
            limits:
              cpu: "2000m"
              memory: "2Gi"
              ephemeral-storage: "4Gi"
          volumeMounts:
            - name: model-volume
              mountPath: /models
              readOnly: true
      volumes:
        - name: model-volume
          configMap:
            name: ${CONFIGMAP_NAME}
---
apiVersion: v1
kind: Service
metadata:
  name: ${SERVICE_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: greenai-inference
    eval_id_k8s: ${K8S_EVAL_ID}
spec:
  type: ClusterIP
  selector:
    app: greenai-inference
    eval_id_k8s: ${K8S_EVAL_ID}
  ports:
    - name: http
      port: 8080
      targetPort: 8080
---
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: greenai-inference-benchmark
    eval_id_k8s: ${K8S_EVAL_ID}
spec:
  backoffLimit: 0
  template:
    metadata:
      labels:
        app: greenai-inference-benchmark
        eval_id_k8s: ${K8S_EVAL_ID}
    spec:
      restartPolicy: Never
      nodeSelector:
        kubernetes.io/hostname: ${BENCHMARK_NODE}
      containers:
        - name: benchmark
          image: ${IMAGE}
          imagePullPolicy: ${IMAGE_PULL_POLICY}
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -e
              python3 /app/scripts/benchmark_inference_http_local.py \
                --url=http://${SERVICE_NAME}.${NAMESPACE}.svc.cluster.local:8080 \
                --eval-id=${EVAL_ID} \
                --train-run-id=${TRAIN_RUN} \
                --inference-node=${INFERENCE_NODE} \
                --benchmark-node=${BENCHMARK_NODE} \
                --samples=${SAMPLES} \
                --warmup=${WARMUP} \
                --health-pings=${HEALTH_PINGS} \
                --concurrency=${CONCURRENCY} \
                --timeout=${TIMEOUT} \
                --download \
                --out-dir=/results
              echo "__GREENAI_BENCHMARK_DONE__"
              sleep ${RESULT_HOLD_SECONDS}
          env:
            - name: NODE_NAME
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: POD_NAMESPACE
              valueFrom:
                fieldRef:
                  fieldPath: metadata.namespace
          resources:
            requests:
              cpu: "300m"
              memory: "512Mi"
              ephemeral-storage: "2Gi"
            limits:
              cpu: "2000m"
              memory: "2Gi"
              ephemeral-storage: "6Gi"
EOF

echo "Wende Manifest an..."
kubectl apply -f "$MANIFEST_PATH"

echo "Warte auf Inference Deployment..."
kubectl rollout status deployment/"$DEPLOYMENT_NAME" -n "$NAMESPACE" --timeout=5m

echo "Warte auf Benchmark Pod..."
BENCH_POD=""
for i in $(seq 1 120); do
  BENCH_POD="$(kubectl get pod -n "$NAMESPACE" -l job-name="$JOB_NAME" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [ -n "$BENCH_POD" ]; then
    break
  fi
  sleep 1
done

if [ -z "$BENCH_POD" ]; then
  echo "Fehler: Benchmark-Pod wurde nicht gefunden."
  kubectl get pods -n "$NAMESPACE" -o wide
  exit 1
fi

echo "Benchmark Pod: $BENCH_POD"

echo "Warte auf Benchmark-Ergebnis..."
DONE=0
for i in $(seq 1 1800); do
  kubectl logs -n "$NAMESPACE" "$BENCH_POD" > "${OUT_DIR}/benchmark.log" 2>&1 || true

  if grep -q "__GREENAI_BENCHMARK_DONE__" "${OUT_DIR}/benchmark.log"; then
    DONE=1
    break
  fi

  PHASE="$(kubectl get pod -n "$NAMESPACE" "$BENCH_POD" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  if [ "$PHASE" = "Failed" ] || [ "$PHASE" = "Succeeded" ]; then
    echo "Benchmark-Pod ist beendet, aber Marker wurde nicht gefunden. Phase: $PHASE"
    cat "${OUT_DIR}/benchmark.log" || true
    kubectl describe pod -n "$NAMESPACE" "$BENCH_POD" || true
    exit 1
  fi

  sleep 2
done

if [ "$DONE" != "1" ]; then
  echo "Timeout: Benchmark-Ergebnis wurde nicht rechtzeitig erzeugt."
  cat "${OUT_DIR}/benchmark.log" || true
  exit 1
fi

cat "${OUT_DIR}/benchmark.log"

echo "Kopiere Ergebnisdateien, solange der Pod noch läuft..."
kubectl cp -n "$NAMESPACE" "${BENCH_POD}:/results/inference_summary.json" "${OUT_DIR}/inference_summary.json"
kubectl cp -n "$NAMESPACE" "${BENCH_POD}:/results/inference_predictions.jsonl" "${OUT_DIR}/inference_predictions.jsonl"

echo "Summary:"
cat "${OUT_DIR}/inference_summary.json"

if [ "$EXPORT_INFLUX" = "1" ]; then
  echo "---"
  echo "Schreibe Inference-Performance nach InfluxDB..."
  python3 scripts/write_inference_metrics_to_influx.py \
    "${OUT_DIR}/inference_summary.json" \
    --train-run-dir "$TRAIN_RUN_DIR" \
    --eval-id "$EVAL_ID" \
    --mode k3s_http \
    --inference-node "$INFERENCE_NODE" \
    --benchmark-node "$BENCHMARK_NODE" \
    --batch-size 1
fi

if [ "$RUN_ENERGY_SUMMARY" = "1" ]; then
  echo "---"
  echo "Berechne Shelly-Energie für Inference..."
  python3 scripts/energy_summary_inference.py \
    "${OUT_DIR}/inference_summary.json" \
    --train-run-dir "$TRAIN_RUN_DIR" \
    --node-mapping node_mapping.json \
    --eval-id "$EVAL_ID" \
    --mode k3s_http \
    --inference-node "$INFERENCE_NODE" \
    --benchmark-node "$BENCHMARK_NODE"
fi

if [ "$CLEANUP" = "1" ]; then
  echo "---"
  echo "Räume K3S-Objekte auf..."
  kubectl delete job "$JOB_NAME" -n "$NAMESPACE" --ignore-not-found=true
  kubectl delete deployment "$DEPLOYMENT_NAME" -n "$NAMESPACE" --ignore-not-found=true
  kubectl delete service "$SERVICE_NAME" -n "$NAMESPACE" --ignore-not-found=true
  kubectl delete configmap "$CONFIGMAP_NAME" -n "$NAMESPACE" --ignore-not-found=true
else
  echo "---"
  echo "K3S-Objekte bleiben zur Kontrolle bestehen."
  echo "Der Benchmark-Job endet automatisch nach RESULT_HOLD_SECONDS=${RESULT_HOLD_SECONDS}."
  echo "Aufräumen später mit:"
  echo "kubectl delete job ${JOB_NAME} deployment ${DEPLOYMENT_NAME} service ${SERVICE_NAME} configmap ${CONFIGMAP_NAME} -n ${NAMESPACE} --ignore-not-found=true"
fi

echo "---"
echo "Fertig."
echo "Eval ID: $EVAL_ID"
echo "Ergebnisse: $OUT_DIR"
