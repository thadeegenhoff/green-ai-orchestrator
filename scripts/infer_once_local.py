#!/usr/bin/env python3

import argparse
import json
import time
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import datasets, transforms


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
    """Gleiche Architektur wie in quickstart-pytorch/pytorchexample/task.py."""

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="Pfad zu final_model.pt")
    parser.add_argument("--data-dir", default="data/cifar10", help="CIFAR-10 Cache-Verzeichnis")
    parser.add_argument("--sample-index", type=int, default=0, help="Index aus dem CIFAR-10-Testset")
    parser.add_argument("--download", action="store_true", help="CIFAR-10 herunterladen, falls nicht vorhanden")
    args = parser.parse_args()

    model_path = Path(args.model)
    if not model_path.exists():
        raise FileNotFoundError(f"Modell nicht gefunden: {model_path}")

    transform = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
        ]
    )

    dataset = datasets.CIFAR10(
        root=args.data_dir,
        train=False,
        download=args.download,
        transform=transform,
    )

    image, true_label = dataset[args.sample_index]

    model = Net()
    state = torch.load(model_path, map_location="cpu")
    model.load_state_dict(state, strict=True)
    model.eval()

    x = image.unsqueeze(0)

    start = time.perf_counter()
    with torch.inference_mode():
        logits = model(x)
        probabilities = torch.softmax(logits, dim=1)
    end = time.perf_counter()

    predicted_label = int(probabilities.argmax(dim=1).item())
    confidence = float(probabilities[0, predicted_label].item())
    inference_ms = (end - start) * 1000.0

    result = {
        "model_path": str(model_path),
        "sample_index": args.sample_index,
        "true_label": int(true_label),
        "true_class": CIFAR10_CLASSES[int(true_label)],
        "predicted_label": predicted_label,
        "predicted_class": CIFAR10_CLASSES[predicted_label],
        "confidence": confidence,
        "correct": bool(predicted_label == int(true_label)),
        "model_inference_ms": inference_ms,
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
