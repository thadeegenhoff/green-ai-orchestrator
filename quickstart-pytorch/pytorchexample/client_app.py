"""pytorchexample: A flexible Flower / PyTorch ClientApp."""

import torch
from flwr.app import ArrayRecord, Context, Message, MetricRecord, RecordDict
from flwr.clientapp import ClientApp

from pytorchexample.task import Net, load_data
from pytorchexample.task import evaluate_metrics as evaluate_metrics_fn
from pytorchexample.task import train as train_fn


# Flower ClientApp
app = ClientApp()


def get_config_value(msg: Message, context: Context, key: str, default):
    """Read config from server message first, then from run_config."""
    try:
        config = msg.content["config"]
        if key in config:
            return config[key]
    except Exception:
        pass

    return context.run_config.get(key, default)


@app.train()
def train(msg: Message, context: Context):
    """Train the model on local data."""

    # Load the model and initialize it with the received weights
    model = Net()
    model.load_state_dict(msg.content["arrays"].to_torch_state_dict())
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model.to(device)

    # Load config
    partition_id = int(context.node_config["partition-id"])
    num_partitions = int(context.node_config["num-partitions"])
    batch_size = int(get_config_value(msg, context, "batch-size", 32))
    local_epochs = int(get_config_value(msg, context, "local-epochs", 1))

    # Keep compatibility with the existing quickstart key "lr"
    lr = float(
        get_config_value(
            msg,
            context,
            "lr",
            get_config_value(msg, context, "learning-rate", 0.1),
        )
    )

    # Load local data partition
    trainloader, _ = load_data(
        partition_id=partition_id,
        num_partitions=num_partitions,
        batch_size=batch_size,
        cfg=context.run_config,
    )

    # Call the training function
    train_loss = train_fn(
        model,
        trainloader,
        local_epochs,
        lr,
        device,
    )

    # Construct and return reply Message
    model_record = ArrayRecord(model.state_dict())
    metrics = {
        "train_loss": train_loss,
        "num-examples": len(trainloader.dataset),
        "partition_id": partition_id,
    }
    metric_record = MetricRecord(metrics)
    content = RecordDict({"arrays": model_record, "metrics": metric_record})
    return Message(content=content, reply_to=msg)


@app.evaluate()
def evaluate(msg: Message, context: Context):
    """Evaluate the model on local data."""

    # Load the model and initialize it with the received weights
    model = Net()
    model.load_state_dict(msg.content["arrays"].to_torch_state_dict())
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model.to(device)

    # Load config
    partition_id = int(context.node_config["partition-id"])
    num_partitions = int(context.node_config["num-partitions"])
    batch_size = int(get_config_value(msg, context, "batch-size", 32))

    # Load local data partition
    _, valloader = load_data(
        partition_id=partition_id,
        num_partitions=num_partitions,
        batch_size=batch_size,
        cfg=context.run_config,
    )

    # Call the evaluation function
    eval_metrics = evaluate_metrics_fn(
        model,
        valloader,
        device,
    )
    eval_loss = float(eval_metrics["loss"])
    eval_acc = float(eval_metrics["accuracy"])
    loss = eval_loss
    accuracy = eval_acc
    test_loss = eval_loss
    test_acc = eval_acc
    # Construct and return reply Message
    metrics = {
        "eval_loss": eval_loss,
        "eval_acc": eval_acc,
        "num-examples": len(valloader.dataset),
        "partition_id": partition_id,
    }
    metric_record = MetricRecord(metrics)
    content = RecordDict({"metrics": metric_record})

    metrics.update(
        {
            "eval_loss": float(eval_metrics["loss"]),
            "eval_acc": float(eval_metrics["accuracy"]),
            "eval_precision_macro": float(eval_metrics["precision_macro"]),
            "eval_recall_macro": float(eval_metrics["recall_macro"]),
            "eval_f1_macro": float(eval_metrics["f1_macro"]),
            "eval_precision_weighted": float(eval_metrics["precision_weighted"]),
            "eval_recall_weighted": float(eval_metrics["recall_weighted"]),
            "eval_f1_weighted": float(eval_metrics["f1_weighted"]),
            "num-examples": len(valloader.dataset),
        }
    )

    return Message(content=content, reply_to=msg)
