apiVersion: v1
kind: Namespace
metadata:
  name: flwr
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: flower-superlink
  namespace: flwr
  labels:
    app: flower-superlink
    run_id: "${K8S_RUN_ID}"
    fl_role: server
spec:
  replicas: 1
  selector:
    matchLabels:
      app: flower-superlink
  template:
    metadata:
      labels:
        app: flower-superlink
        run_id: "${K8S_RUN_ID}"
        fl_role: server
        greenai.local/fl-role: server
        greenai.local/workload-zone: control
    spec:
      nodeSelector:
        greenai.local/fl-role: server
        greenai.local/workload-zone: control
      tolerations:
        - key: "node-role.kubernetes.io/control-plane"
          operator: "Exists"
          effect: "NoSchedule"
        - key: "node-role.kubernetes.io/master"
          operator: "Exists"
          effect: "NoSchedule"
      containers:
        - name: superlink
          image: ${SUPERLINK_IMAGE}
          imagePullPolicy: IfNotPresent
          args:
            - "--insecure"
            - "--isolation"
            - "process"
          ports:
            - name: serverappio
              containerPort: 9091
            - name: fleet
              containerPort: 9092
            - name: control
              containerPort: 9093
          env:
            - name: RUN_ID
              value: "${RUN_ID}"
            - name: K8S_RUN_ID
              value: "${K8S_RUN_ID}"
            - name: STRATEGY
              value: "${STRATEGY}"
            - name: DATASET
              value: "${DATASET}"
            - name: MODEL_NAME
              value: "${MODEL_NAME}"
            - name: NUM_ROUNDS
              value: "${NUM_ROUNDS}"
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
              cpu: "250m"
              memory: "256Mi"
              ephemeral-storage: "512Mi"
            limits:
              cpu: "1000m"
              memory: "1Gi"
              ephemeral-storage: "2Gi"

        - name: superexec-serverapp
          image: ${APP_IMAGE}
          imagePullPolicy: Always
          command: ["/bin/sh", "-c"]
          args:
            - |
              sleep 5
              exec flower-superexec \
                --insecure \
                --plugin-type serverapp \
                --appio-api-address 127.0.0.1:9091
          env:
            - name: PYTHONUNBUFFERED
              value: "1"
            - name: RUN_ID
              value: "${RUN_ID}"
            - name: K8S_RUN_ID
              value: "${K8S_RUN_ID}"
            - name: STRATEGY
              value: "${STRATEGY}"
            - name: DATASET
              value: "${DATASET}"
            - name: MODEL_NAME
              value: "${MODEL_NAME}"
            - name: NUM_ROUNDS
              value: "${NUM_ROUNDS}"
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
              cpu: "500m"
              memory: "1Gi"
              ephemeral-storage: "2Gi"
            limits:
              cpu: "2000m"
              memory: "4Gi"
              ephemeral-storage: "8Gi"
---
apiVersion: v1
kind: Service
metadata:
  name: flower-superlink
  namespace: flwr
  labels:
    app: flower-superlink
    run_id: "${K8S_RUN_ID}"
    fl_role: server
spec:
  selector:
    app: flower-superlink
    run_id: "${K8S_RUN_ID}"
  ports:
    - name: serverappio
      port: 9091
      targetPort: 9091
    - name: fleet
      port: 9092
      targetPort: 9092
    - name: control
      port: 9093
      targetPort: 9093
---
${CLIENT_WORKLOADS}
