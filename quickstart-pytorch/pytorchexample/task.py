"""pytorchexample: A flexible Flower / PyTorch app."""

import torch
import torch.nn as nn
import torch.nn.functional as F
from datasets import load_dataset
from flwr_datasets import FederatedDataset
from flwr_datasets.partitioner import (
    DirichletPartitioner,
    IidPartitioner,
    PathologicalPartitioner,
)
from torch.utils.data import DataLoader
from torchvision.transforms import Compose, Normalize, ToTensor


class Net(nn.Module):
    """Model: simple CNN adapted from PyTorch CIFAR-10 examples."""

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


# Cache FederatedDataset instances per configuration
fds_cache = {}

pytorch_transforms = Compose(
    [
        ToTensor(),
        Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
    ]
)


def cfg_get(cfg, key: str, default):
    """Read value from Flower run_config with a fallback."""
    if cfg is None:
        return default
    return cfg.get(key, default)


def bool_from_cfg(value) -> bool:
    """Convert common config values to bool."""
    if isinstance(value, bool):
        return value
    return str(value).lower() in ["1", "true", "yes", "y"]


def resolve_dataset_name(dataset_name: str) -> str:
    """Resolve short dataset aliases to Hugging Face dataset names."""
    dataset_name = str(dataset_name).lower()

    if dataset_name in ["cifar10", "cifar-10"]:
        return "uoft-cs/cifar10"

    return dataset_name


def build_partitioner(split: str, num_partitions: int, cfg):
    """Build a Flower Datasets partitioner from run_config."""
    split = str(split).lower()

    if split in ["iid", "weighted_iid"]:
        return IidPartitioner(num_partitions=num_partitions)

    if split in ["dirichlet", "non_iid_dirichlet"]:
        return DirichletPartitioner(
            num_partitions=num_partitions,
            partition_by="label",
            alpha=float(cfg_get(cfg, "dirichlet-alpha", 0.5)),
            min_partition_size=int(cfg_get(cfg, "min-partition-size", 10)),
            self_balancing=bool_from_cfg(cfg_get(cfg, "self-balancing", True)),
            seed=int(cfg_get(cfg, "partition-seed", 42)),
        )

    if split in ["pathological", "label_shard"]:
        return PathologicalPartitioner(
            num_partitions=num_partitions,
            partition_by="label",
            num_classes_per_partition=int(cfg_get(cfg, "classes-per-partition", 2)),
            class_assignment_mode=str(
                cfg_get(cfg, "class-assignment-mode", "first-deterministic")
            ),
            seed=int(cfg_get(cfg, "partition-seed", 42)),
        )

    raise ValueError(
        f"Unknown split '{split}'. "
        "Use one of: iid, weighted_iid, dirichlet, pathological."
    )


def apply_transforms(batch):
    """Apply transforms to the partition from FederatedDataset."""
    batch["img"] = [pytorch_transforms(img) for img in batch["img"]]
    return batch


def load_data(partition_id: int, num_partitions: int, batch_size: int, cfg=None):
    """Load a local CIFAR-10 partition."""
    dataset_name = resolve_dataset_name(cfg_get(cfg, "dataset", "cifar10"))
    split = str(cfg_get(cfg, "split", "iid")).lower()

    cache_key = (
        dataset_name,
        split,
        num_partitions,
        str(cfg_get(cfg, "dirichlet-alpha", "")),
        str(cfg_get(cfg, "min-partition-size", "")),
        str(cfg_get(cfg, "classes-per-partition", "")),
        str(cfg_get(cfg, "partition-seed", "")),
    )

    if cache_key not in fds_cache:
        print(
            f"Initializing FederatedDataset: "
            f"dataset={dataset_name}, split={split}, "
            f"num_partitions={num_partitions}"
        )

        partitioner = build_partitioner(
            split=split,
            num_partitions=num_partitions,
            cfg=cfg,
        )

        fds_cache[cache_key] = FederatedDataset(
            dataset=dataset_name,
            partitioners={"train": partitioner},
        )

    fds = fds_cache[cache_key]

    partition = fds.load_partition(partition_id)

    # Divide data on each node: 80% train, 20% validation
    partition_train_test = partition.train_test_split(test_size=0.2, seed=42)

    # Construct dataloaders
    partition_train_test = partition_train_test.with_transform(apply_transforms)

    trainloader = DataLoader(
        partition_train_test["train"],
        batch_size=batch_size,
        shuffle=True,
    )
    testloader = DataLoader(
        partition_train_test["test"],
        batch_size=batch_size,
    )

    return trainloader, testloader


def load_centralized_dataset():
    """Load centralized CIFAR-10 test set and return dataloader."""
    test_dataset = load_dataset("uoft-cs/cifar10", split="test")
    dataset = test_dataset.with_format("torch").with_transform(apply_transforms)
    return DataLoader(dataset, batch_size=128)


def train(net, trainloader, epochs, lr, device):
    """Train the model on the training set."""
    net.to(device)
    criterion = torch.nn.CrossEntropyLoss().to(device)
    optimizer = torch.optim.SGD(net.parameters(), lr=lr, momentum=0.9)
    net.train()

    running_loss = 0.0

    for _ in range(epochs):
        for batch in trainloader:
            images = batch["img"].to(device)
            labels = batch["label"].to(device)

            optimizer.zero_grad()
            loss = criterion(net(images), labels)
            loss.backward()
            optimizer.step()

            running_loss += loss.item()

    avg_trainloss = running_loss / (epochs * len(trainloader))
    return avg_trainloss


def _classification_metrics_from_confusion_matrix(confusion_matrix: torch.Tensor):
    """Calculate macro and weighted precision/recall/F1 from a confusion matrix."""
    cm = confusion_matrix.to(torch.float64)

    tp = torch.diag(cm)
    predicted_per_class = cm.sum(dim=0)
    actual_per_class = cm.sum(dim=1)

    precision_per_class = torch.where(
        predicted_per_class > 0,
        tp / predicted_per_class,
        torch.zeros_like(tp),
    )
    recall_per_class = torch.where(
        actual_per_class > 0,
        tp / actual_per_class,
        torch.zeros_like(tp),
    )
    f1_per_class = torch.where(
        (precision_per_class + recall_per_class) > 0,
        2 * precision_per_class * recall_per_class / (precision_per_class + recall_per_class),
        torch.zeros_like(tp),
    )

    support = actual_per_class
    total_support = support.sum()

    if total_support > 0:
        weighted_precision = (precision_per_class * support).sum() / total_support
        weighted_recall = (recall_per_class * support).sum() / total_support
        weighted_f1 = (f1_per_class * support).sum() / total_support
    else:
        weighted_precision = torch.tensor(0.0, dtype=torch.float64)
        weighted_recall = torch.tensor(0.0, dtype=torch.float64)
        weighted_f1 = torch.tensor(0.0, dtype=torch.float64)

    return {
        "precision_macro": float(precision_per_class.mean().item()),
        "recall_macro": float(recall_per_class.mean().item()),
        "f1_macro": float(f1_per_class.mean().item()),
        "precision_weighted": float(weighted_precision.item()),
        "recall_weighted": float(weighted_recall.item()),
        "f1_weighted": float(weighted_f1.item()),
    }


def evaluate_metrics(net, testloader, device, num_classes: int = 10):
    """Validate the model and return loss, accuracy, precision, recall and F1."""
    net.to(device)
    criterion = torch.nn.CrossEntropyLoss()
    net.eval()

    correct = 0
    total = 0
    loss = 0.0
    confusion_matrix = torch.zeros(num_classes, num_classes, dtype=torch.int64)

    with torch.no_grad():
        for batch in testloader:
            images = batch["img"].to(device)
            labels = batch["label"].to(device)

            outputs = net(images)
            batch_loss = criterion(outputs, labels)
            loss += batch_loss.item()

            predictions = torch.max(outputs.data, 1)[1]
            correct += (predictions == labels).sum().item()
            total += labels.numel()

            for true_label, predicted_label in zip(labels.cpu(), predictions.cpu()):
                confusion_matrix[int(true_label), int(predicted_label)] += 1

    avg_loss = loss / len(testloader) if len(testloader) > 0 else 0.0
    accuracy = correct / total if total > 0 else 0.0

    metrics = _classification_metrics_from_confusion_matrix(confusion_matrix)
    metrics.update(
        {
            "loss": float(avg_loss),
            "accuracy": float(accuracy),
            "num_examples": float(total),
        }
    )

    return metrics


def test(net, testloader, device):
    """Validate the model on the test set.

    Kept for backwards compatibility with existing client/server code.
    New code should prefer evaluate_metrics(...).
    """
    metrics = evaluate_metrics(net, testloader, device)
    return metrics["loss"], metrics["accuracy"]
