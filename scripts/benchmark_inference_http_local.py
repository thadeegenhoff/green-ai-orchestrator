#!/usr/bin/env python3

import argparse
import asyncio
import io
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from torchvision import datasets


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


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return float(values[f])
    return float(values[f] * (c - k) + values[c] * (k - f))


def pil_to_png_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def health_ping(client, url):
    start = time.perf_counter()
    response = await client.get(f"{url}/health")
    end = time.perf_counter()
    response.raise_for_status()
    return float((end - start) * 1000.0)


async def send_one(client, url, sample_id, image_bytes, true_label):
    start = time.perf_counter()

    try:
        files = {
            "file": (
                f"sample_{sample_id}.png",
                image_bytes,
                "image/png",
            )
        }

        response = await client.post(f"{url}/predict", files=files)
        end = time.perf_counter()

        latency_ms = float((end - start) * 1000.0)
        status_code = response.status_code

        if status_code != 200:
            return {
                "sample_id": sample_id,
                "true_label": int(true_label),
                "true_class": CIFAR10_CLASSES[int(true_label)],
                "ok": False,
                "http_status": status_code,
                "latency_ms": latency_ms,
                "error": f"http_status_{status_code}",
            }

        data = response.json()
        predicted_label = data.get("predicted_label")
        correct = predicted_label == int(true_label)

        return {
            "sample_id": sample_id,
            "true_label": int(true_label),
            "true_class": CIFAR10_CLASSES[int(true_label)],
            "ok": bool(data.get("ok", False)),
            "http_status": status_code,
            "predicted_label": predicted_label,
            "predicted_class": data.get("predicted_class"),
            "confidence": float(data.get("confidence", 0.0)),
            "correct": bool(correct),
            "latency_ms": latency_ms,
            "model_inference_ms": float(data.get("model_inference_ms", 0.0)),
            "error": data.get("error", ""),
        }

    except Exception as exc:
        end = time.perf_counter()
        return {
            "sample_id": sample_id,
            "true_label": int(true_label),
            "true_class": CIFAR10_CLASSES[int(true_label)],
            "ok": False,
            "http_status": 0,
            "latency_ms": float((end - start) * 1000.0),
            "error": str(exc),
        }


async def run_benchmark(args):
    url = args.url.rstrip("/")

    dataset = datasets.CIFAR10(
        root=args.data_dir,
        train=False,
        download=args.download,
        transform=None,
    )

    samples = []
    for i in range(args.samples):
        image, label = dataset[i]
        samples.append((i, pil_to_png_bytes(image), int(label)))

    timeout = httpx.Timeout(args.timeout)
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)

    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        health_latencies = []
        for _ in range(args.health_pings):
            health_latencies.append(await health_ping(client, url))

        # Warmup: Requests werden nicht in die Ergebnisliste aufgenommen
        for i in range(args.warmup):
            sample_id, image_bytes, true_label = samples[i % len(samples)]
            await send_one(client, url, sample_id, image_bytes, true_label)

        semaphore = asyncio.Semaphore(args.concurrency)

        async def guarded_send(item):
            async with semaphore:
                sample_id, image_bytes, true_label = item
                return await send_one(client, url, sample_id, image_bytes, true_label)

        benchmark_start_dt = datetime.now(timezone.utc)
        start_total = time.perf_counter()

        results = await asyncio.gather(*(guarded_send(item) for item in samples))

        end_total = time.perf_counter()
        benchmark_end_dt = datetime.now(timezone.utc)

    successful = [r for r in results if r.get("ok") is True and r.get("http_status") == 200]
    failed = [r for r in results if r not in successful]

    correct = [r for r in successful if r.get("correct") is True]

    latency_values = [r["latency_ms"] for r in successful if "latency_ms" in r]
    model_values = [r["model_inference_ms"] for r in successful if "model_inference_ms" in r]

    measured_seconds = max(end_total - start_total, 0.000001)

    summary = {
        "schema_version": 1,
        "url": url,
        "dataset": "cifar10",
        "protocol": "http",
        "eval_id": args.eval_id,
        "train_run_id": args.train_run_id,
        "inference_node": args.inference_node,
        "benchmark_node": args.benchmark_node,
        "benchmark_start_time": benchmark_start_dt.isoformat().replace("+00:00", "Z"),
        "benchmark_end_time": benchmark_end_dt.isoformat().replace("+00:00", "Z"),
        "benchmark_start_ns": float(int(benchmark_start_dt.timestamp() * 1_000_000_000)),
        "benchmark_end_ns": float(int(benchmark_end_dt.timestamp() * 1_000_000_000)),
        "samples": float(args.samples),
        "requests": float(len(results)),
        "warmup_requests": float(args.warmup),
        "health_pings": float(args.health_pings),
        "concurrency": float(args.concurrency),
        "successful_requests": float(len(successful)),
        "failed_requests": float(len(failed)),
        "technical_success_rate": float(len(successful) / len(results)) if results else 0.0,
        "top1_accuracy": float(len(correct) / len(successful)) if successful else 0.0,
        "measured_seconds": float(measured_seconds),
        "throughput_req_per_sec": float(len(successful) / measured_seconds),
        "health_ping_ms_avg": float(statistics.mean(health_latencies)) if health_latencies else None,
        "health_ping_ms_p50": percentile(health_latencies, 50),
        "health_ping_ms_p95": percentile(health_latencies, 95),
        "latency_ms_avg": float(statistics.mean(latency_values)) if latency_values else None,
        "latency_ms_p50": percentile(latency_values, 50),
        "latency_ms_p90": percentile(latency_values, 90),
        "latency_ms_p95": percentile(latency_values, 95),
        "latency_ms_p99": percentile(latency_values, 99),
        "model_inference_ms_avg": float(statistics.mean(model_values)) if model_values else None,
        "model_inference_ms_p50": percentile(model_values, 50),
        "model_inference_ms_p95": percentile(model_values, 95),
        "model_inference_ms_p99": percentile(model_values, 99),
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary_path = out_dir / "inference_summary.json"
    predictions_path = out_dir / "inference_predictions.jsonl"

    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    with predictions_path.open("w", encoding="utf-8") as f:
        for row in results:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Summary geschrieben: {summary_path}")
    print(f"Predictions geschrieben: {predictions_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--eval-id", default="local_http")
    parser.add_argument("--train-run-id", default="")
    parser.add_argument("--inference-node", default="greenai-control")
    parser.add_argument("--benchmark-node", default="greenai-control")
    parser.add_argument("--data-dir", default="data/cifar10")
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--health-pings", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--out-dir", default="runs/inference_local_test")
    args = parser.parse_args()

    asyncio.run(run_benchmark(args))


if __name__ == "__main__":
    main()
