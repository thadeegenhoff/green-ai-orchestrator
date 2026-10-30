# Set GREENAI_APP_IMAGE to an image built from this repository before running the controller.
# Application image values below are local placeholders, not published images.
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
        node_target: greenai-control
    spec:
      nodeSelector:
        kubernetes.io/hostname: greenai-control
      tolerations:
        - key: "node-role.kubernetes.io/control-plane"
          operator: "Exists"
          effect: "NoSchedule"
        - key: "node-role.kubernetes.io/master"
          operator: "Exists"
          effect: "NoSchedule"
      containers:
        - name: superlink
          image: flwr/superlink:1.29.0
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
              cpu: "100m"
              memory: "128Mi"
              ephemeral-storage: "128Mi"

        - name: superexec-serverapp
          image: greenai-review:local
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
              cpu: "100m"
              memory: "256Mi"
              ephemeral-storage: "256Mi"
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
apiVersion: apps/v1
kind: Deployment
metadata:
  name: flower-client-cloud-gpu
  namespace: flwr
  labels:
    app: flower-client
    run_id: "${K8S_RUN_ID}"
    fl_role: client
    node_target: cloud-gpu
spec:
  replicas: 1
  selector:
    matchLabels:
      client_app: flower-client-cloud-gpu
  template:
    metadata:
      labels:
        app: flower-client
        client_app: flower-client-cloud-gpu
        run_id: "${K8S_RUN_ID}"
        fl_role: client
        node_target: cloud-gpu
    spec:
      nodeSelector:
        kubernetes.io/hostname: cloud-gpu
      containers:
        - name: supernode
          image: flwr/supernode:1.29.0
          imagePullPolicy: IfNotPresent
          command: ["/bin/sh", "-c"]
          args:
            - |
              echo "Starting Flower SuperNode on cloud-gpu with partition-id=0 num-partitions=4"

              exec flower-supernode \
                --insecure \
                --superlink flower-superlink.flwr.svc.cluster.local:9092 \
                --node-config "partition-id=0 num-partitions=4" \
                --clientappio-api-address 0.0.0.0:9094 \
                --isolation process
          ports:
            - name: clientappio
              containerPort: 9094
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
            - name: PARTITION_ID
              value: "0"
            - name: NUM_PARTITIONS
              value: "4"
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
              cpu: "100m"
              memory: "128Mi"
              ephemeral-storage: "128Mi"

        - name: superexec-clientapp
          image: greenai-review:local
          imagePullPolicy: Always
          command: ["/bin/sh", "-c"]
          args:
            - |
              sleep 8
              exec flower-superexec \
                --insecure \
                --plugin-type clientapp \
                --appio-api-address 127.0.0.1:9094
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
            - name: PARTITION_ID
              value: "0"
            - name: NUM_PARTITIONS
              value: "4"
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
              memory: "512Mi"
              ephemeral-storage: "2Gi"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: flower-client-old-laptop
  namespace: flwr
  labels:
    app: flower-client
    run_id: "${K8S_RUN_ID}"
    fl_role: client
    node_target: old-laptop
spec:
  replicas: 1
  selector:
    matchLabels:
      client_app: flower-client-old-laptop
  template:
    metadata:
      labels:
        app: flower-client
        client_app: flower-client-old-laptop
        run_id: "${K8S_RUN_ID}"
        fl_role: client
        node_target: old-laptop
    spec:
      nodeSelector:
        kubernetes.io/hostname: old-laptop
      containers:
        - name: supernode
          image: flwr/supernode:1.29.0
          imagePullPolicy: IfNotPresent
          command: ["/bin/sh", "-c"]
          args:
            - |
              echo "Starting Flower SuperNode on old-laptop with partition-id=1 num-partitions=4"

              exec flower-supernode \
                --insecure \
                --superlink flower-superlink.flwr.svc.cluster.local:9092 \
                --node-config "partition-id=1 num-partitions=4" \
                --clientappio-api-address 0.0.0.0:9094 \
                --isolation process
          ports:
            - name: clientappio
              containerPort: 9094
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
            - name: PARTITION_ID
              value: "1"
            - name: NUM_PARTITIONS
              value: "4"
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
              cpu: "100m"
              memory: "128Mi"
              ephemeral-storage: "128Mi"

        - name: superexec-clientapp
          image: greenai-review:local
          imagePullPolicy: Always
          command: ["/bin/sh", "-c"]
          args:
            - |
              sleep 8
              exec flower-superexec \
                --insecure \
                --plugin-type clientapp \
                --appio-api-address 127.0.0.1:9094
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
            - name: PARTITION_ID
              value: "1"
            - name: NUM_PARTITIONS
              value: "4"
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
              memory: "512Mi"
              ephemeral-storage: "2Gi"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: flower-client-pi5-16gb
  namespace: flwr
  labels:
    app: flower-client
    run_id: "${K8S_RUN_ID}"
    fl_role: client
    node_target: pi5-16gb
spec:
  replicas: 1
  selector:
    matchLabels:
      client_app: flower-client-pi5-16gb
  template:
    metadata:
      labels:
        app: flower-client
        client_app: flower-client-pi5-16gb
        run_id: "${K8S_RUN_ID}"
        fl_role: client
        node_target: pi5-16gb
    spec:
      nodeSelector:
        kubernetes.io/hostname: pi5-16gb
      containers:
        - name: supernode
          image: flwr/supernode:1.29.0
          imagePullPolicy: IfNotPresent
          command: ["/bin/sh", "-c"]
          args:
            - |
              echo "Starting Flower SuperNode on pi5-16gb with partition-id=2 num-partitions=4"

              exec flower-supernode \
                --insecure \
                --superlink flower-superlink.flwr.svc.cluster.local:9092 \
                --node-config "partition-id=2 num-partitions=4" \
                --clientappio-api-address 0.0.0.0:9094 \
                --isolation process
          ports:
            - name: clientappio
              containerPort: 9094
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
            - name: PARTITION_ID
              value: "2"
            - name: NUM_PARTITIONS
              value: "4"
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
              cpu: "100m"
              memory: "128Mi"
              ephemeral-storage: "128Mi"

        - name: superexec-clientapp
          image: greenai-review:local
          imagePullPolicy: Always
          command: ["/bin/sh", "-c"]
          args:
            - |
              sleep 8
              exec flower-superexec \
                --insecure \
                --plugin-type clientapp \
                --appio-api-address 127.0.0.1:9094
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
            - name: PARTITION_ID
              value: "2"
            - name: NUM_PARTITIONS
              value: "4"
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
              memory: "512Mi"
              ephemeral-storage: "2Gi"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: flower-client-pi5-8gb
  namespace: flwr
  labels:
    app: flower-client
    run_id: "${K8S_RUN_ID}"
    fl_role: client
    node_target: pi5-8gb
spec:
  replicas: 1
  selector:
    matchLabels:
      client_app: flower-client-pi5-8gb
  template:
    metadata:
      labels:
        app: flower-client
        client_app: flower-client-pi5-8gb
        run_id: "${K8S_RUN_ID}"
        fl_role: client
        node_target: pi5-8gb
    spec:
      nodeSelector:
        kubernetes.io/hostname: pi5-8gb
      containers:
        - name: supernode
          image: flwr/supernode:1.29.0
          imagePullPolicy: IfNotPresent
          command: ["/bin/sh", "-c"]
          args:
            - |
              echo "Starting Flower SuperNode on pi5-8gb with partition-id=3 num-partitions=4"

              exec flower-supernode \
                --insecure \
                --superlink flower-superlink.flwr.svc.cluster.local:9092 \
                --node-config "partition-id=3 num-partitions=4" \
                --clientappio-api-address 0.0.0.0:9094 \
                --isolation process
          ports:
            - name: clientappio
              containerPort: 9094
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
            - name: PARTITION_ID
              value: "3"
            - name: NUM_PARTITIONS
              value: "4"
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
              cpu: "100m"
              memory: "128Mi"
              ephemeral-storage: "128Mi"

        - name: superexec-clientapp
          image: greenai-review:local
          imagePullPolicy: Always
          command: ["/bin/sh", "-c"]
          args:
            - |
              sleep 8
              exec flower-superexec \
                --insecure \
                --plugin-type clientapp \
                --appio-api-address 127.0.0.1:9094
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
            - name: PARTITION_ID
              value: "3"
            - name: NUM_PARTITIONS
              value: "4"
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
              memory: "512Mi"
              ephemeral-storage: "2Gi"
