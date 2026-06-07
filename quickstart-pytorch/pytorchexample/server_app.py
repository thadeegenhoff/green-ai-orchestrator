"""pytorchexample: A flexible Flower / PyTorch ServerApp."""

import os
from pathlib import Path

import torch
from flwr.app import ArrayRecord, ConfigRecord, Context, MetricRecord
from flwr.serverapp import Grid, ServerApp
from flwr.serverapp.strategy import FedAdagrad, FedAdam, FedAvg, FedYogi

from pytorchexample.task import Net, evaluate_metrics, load_centralized_dataset


# Create ServerApp
app = ServerApp()


def cfg_str(cfg, key: str, default: str) -> str:
    """Read a string value from the run config."""
    return str(cfg.get(key, default)).lower()


def cfg_int(cfg, key: str, default: int) -> int:
    """Read an integer value from the run config."""
    return int(cfg.get(key, default))


def cfg_float(cfg, key: str, default: float) -> float:
    """Read a float value from the run config."""
    return float(cfg.get(key, default))


def build_strategy(cfg):
    """Build the Flower strategy from the experiment YAML run_config."""

    strategy_name = cfg_str(cfg, "strategy", "fedavg")

    common_kwargs = {
        "fraction_train": cfg_float(cfg, "fraction-train", 1.0),
        "fraction_evaluate": cfg_float(cfg, "fraction-evaluate", 0.5),
        "min_train_nodes": cfg_int(cfg, "min-train-nodes", 2),
        "min_evaluate_nodes": cfg_int(cfg, "min-evaluate-nodes", 2),
        "min_available_nodes": cfg_int(cfg, "min-available-nodes", 2),
        "weighted_by_key": "num-examples",
    }

    if strategy_name == "fedavg":
        return FedAvg(**common_kwargs)

    fedopt_kwargs = {
        **common_kwargs,
        "eta": cfg_float(cfg, "server-learning-rate", 0.1),
        "eta_l": cfg_float(cfg, "client-learning-rate", 0.1),
        "tau": cfg_float(cfg, "tau", 0.001),
    }

    if strategy_name == "fedadagrad":
        return FedAdagrad(**fedopt_kwargs)

    if strategy_name == "fedadam":
        return FedAdam(
            **fedopt_kwargs,
            beta_1=cfg_float(cfg, "beta-1", 0.9),
            beta_2=cfg_float(cfg, "beta-2", 0.99),
        )

    if strategy_name == "fedyogi":
        return FedYogi(
            **fedopt_kwargs,
            beta_1=cfg_float(cfg, "beta-1", 0.9),
            beta_2=cfg_float(cfg, "beta-2", 0.99),
        )

    raise ValueError(
        f"Unknown strategy '{strategy_name}'. "
        "Use one of: fedavg, fedadagrad, fedadam, fedyogi."
    )


@app.main()
def main(grid: Grid, context: Context) -> None:
    """Main entry point for the ServerApp."""

    cfg = context.run_config

    # Read run config from experiment YAML
    strategy_name = cfg_str(cfg, "strategy", "fedavg")
    num_rounds = cfg_int(cfg, "num-server-rounds", 10)
    lr = cfg_float(cfg, "learning-rate", 0.1)
    local_epochs = cfg_int(cfg, "local-epochs", 1)
    batch_size = cfg_int(cfg, "batch-size", 32)
    timeout = cfg_float(cfg, "timeout", 3600.0)

    print("\nStarting flexible Flower experiment")
    print(f"Strategy: {strategy_name}")
    print(f"Rounds: {num_rounds}")
    print(f"Learning rate: {lr}")
    print(f"Local epochs: {local_epochs}")
    print(f"Batch size: {batch_size}")

    # Load global model
    global_model = Net()
    arrays = ArrayRecord(global_model.state_dict())

    # Initialize selected strategy
    strategy = build_strategy(cfg)

    # Config sent to ClientApps.
    # Keep 'lr' because the Flower quickstart client usually expects this key.
    client_config = ConfigRecord(
        {
            "lr": lr,
            "learning-rate": lr,
            "local-epochs": local_epochs,
            "batch-size": batch_size,
            "dataset": str(cfg.get("dataset", "cifar10")),
            "split": str(cfg.get("split", "iid")),
        }
    )

    # Start strategy
    result = strategy.start(
        grid=grid,
        initial_arrays=arrays,
        train_config=client_config,
        evaluate_config=client_config,
        num_rounds=num_rounds,
        timeout=timeout,
        evaluate_fn=global_evaluate,
    )

    # Save final model inside the Flower ServerApp container.
    # It can later be copied into the run directory with kubectl cp.
    model_dir = Path("/tmp/flower-final-model")
    model_dir.mkdir(parents=True, exist_ok=True)

    print("\nSaving final model to disk...")
    print(f"Model output directory: {model_dir}")
    state_dict = result.arrays.to_torch_state_dict()
    model_path = model_dir / "final_model.pt"
    torch.save(state_dict, model_path)
    print(f"Final model saved inside Flower runtime: {model_path}")


def global_evaluate(server_round: int, arrays: ArrayRecord) -> MetricRecord:
    """Evaluate model on central data."""

    # Load the model and initialize it with the received weights
    model = Net()
    model.load_state_dict(arrays.to_torch_state_dict())
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model.to(device)

    # Load entire test set
    test_dataloader = load_centralized_dataset()

    # Evaluate the global model on the test set
    metrics = evaluate_metrics(model, test_dataloader, device)
    # Return the evaluation metrics
    return MetricRecord(
        {
            # Alte Keys behalten, damit bestehende Parser/Dashboards kompatibel bleiben
            "accuracy": float(metrics["accuracy"]),
            "loss": float(metrics["loss"]),

            # Neue zentrale Server-Metriken
            "precision_macro": float(metrics["precision_macro"]),
            "recall_macro": float(metrics["recall_macro"]),
            "f1_macro": float(metrics["f1_macro"]),
            "precision_weighted": float(metrics["precision_weighted"]),
            "recall_weighted": float(metrics["recall_weighted"]),
            "f1_weighted": float(metrics["f1_weighted"]),

            # Explizite server_-Keys für Influx/Grafana
            "server_accuracy": float(metrics["accuracy"]),
            "server_loss": float(metrics["loss"]),
            "server_precision_macro": float(metrics["precision_macro"]),
            "server_recall_macro": float(metrics["recall_macro"]),
            "server_f1_macro": float(metrics["f1_macro"]),
            "server_precision_weighted": float(metrics["precision_weighted"]),
            "server_recall_weighted": float(metrics["recall_weighted"]),
            "server_f1_weighted": float(metrics["f1_weighted"]),
        }
    )