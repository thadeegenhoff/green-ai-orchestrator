#!/usr/bin/env python3

import argparse
import io
import os
import time
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F
from fastapi import FastAPI, File, UploadFile
from PIL import Image
from torchvision import transforms
import uvicorn


CIFAR10_CLASSES = [
    "airplane",
    "automobile",
    "bird",
    "cat",
    "deer",
    "dog",
    "frog",
    "horse",
    "ship",
    "truck",
]


class Net(nn.Module):
    """Gleiche CNN-Architektur wie im Flower/PyTorch-Training."""

    def __init__(self):
        super(Net, self).__init__()
        self.conv1 = nn.Conv2d(3, 6, 5)
        self.pool = nn.MaxPool2d(2, 2)
        self.conv2 = nn.Conv2d(6, 16, 5)
        self.fc1 = nn.Linear(16 * 5 * 5, 120)
        self.fc2 = nn.Linear(120, 84)
        self.fc3 = nn.Linear(84, 10)

    def forward(self, x):
        x = self.pool(F.relu(self.conv1(x)))
        x = self.pool(F.relu(self.conv2(x)))
        x = x.view(-1, 16 * 5 * 5)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        return self.fc3(x)


app = FastAPI(title="GreenAI CIFAR-10 Inference Service")

model = None
device = torch.device("cpu")
model_path_global = None

preprocess = transforms.Compose(
    [
        transforms.Resize((32, 32)),
        transforms.ToTensor(),
        transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
    ]
)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model_loaded": model is not None,
        "model_path": str(model_path_global),
        "device": str(device),
        "node_name": os.environ.get("NODE_NAME", "local"),
        "pod_name": os.environ.get("POD_NAME", "local"),
    }


@app.post("/predict")
async def predict(file: UploadFile = File(...)):
    if model is None:
        return {
            "ok": False,
            "error": "model_not_loaded",
        }

    try:
        raw = await file.read()

        image = Image.open(io.BytesIO(raw)).convert("RGB")
        x = preprocess(image).unsqueeze(0).to(device)

        start = time.perf_counter()
        with torch.inference_mode():
            logits = model(x)
            probabilities = torch.softmax(logits, dim=1)
        end = time.perf_counter()

        predicted_label = int(probabilities.argmax(dim=1).item())
        confidence = float(probabilities[0, predicted_label].item())

        return {
            "ok": True,
            "predicted_label": predicted_label,
            "predicted_class": CIFAR10_CLASSES[predicted_label],
            "confidence": confidence,
            "model_inference_ms": float((end - start) * 1000.0),
            "device": str(device),
            "node_name": os.environ.get("NODE_NAME", "local"),
            "pod_name": os.environ.get("POD_NAME", "local"),
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
        }


def load_model(model_path: Path):
    global model
    global model_path_global

    if not model_path.exists():
        raise FileNotFoundError(f"Modell nicht gefunden: {model_path}")

    net = Net()
    state = torch.load(model_path, map_location="cpu")
    net.load_state_dict(state, strict=True)
    net.to(device)
    net.eval()

    model = net
    model_path_global = model_path

    print(f"Modell geladen: {model_path}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="Pfad zu final_model.pt")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    load_model(Path(args.model))

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
