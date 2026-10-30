"""Device-level energy integration shared by training and inference exports."""

from math import isfinite


def calculate_energy(samples, start_time, end_time, idle_power_w=None):
    """Use the existing trapezoidal rule; missing observations are not zero use.

    The first/last observed powers are extended to the requested window edges.
    ``valid`` describes basic input availability, not meter calibration, clock
    accuracy or adequate sampling density. No maximum-gap threshold is assumed.
    """
    duration_s = (end_time - start_time).total_seconds()
    result = {
        "duration_s": float(duration_s),
        "sample_count": len(samples),
        "valid": False,
        "measurement_status": "invalid_window",
        "energy_wh": None,
        "net_energy_wh": None,
        "avg_power_w": None,
        "max_power_w": None,
        "min_power_w": None,
    }
    if duration_s <= 0:
        return result
    if len(samples) < 2:
        result["measurement_status"] = "insufficient_samples"
        return result

    previous = start_time
    for timestamp, power in samples:
        if not start_time <= timestamp <= end_time or timestamp < previous:
            result["measurement_status"] = "invalid_sample_times"
            return result
        if not isfinite(float(power)):
            result["measurement_status"] = "non_finite_power"
            return result
        previous = timestamp
    if samples[0][0] == samples[-1][0]:
        result["measurement_status"] = "insufficient_distinct_sample_times"
        return result

    points = [(start_time, samples[0][1])] + list(samples) + [(end_time, samples[-1][1])]
    energy_wh = 0.0
    for (t1, p1), (t2, p2) in zip(points, points[1:]):
        delta_s = (t2 - t1).total_seconds()
        if delta_s > 0:
            energy_wh += (float(p1) + float(p2)) / 2.0 * delta_s / 3600.0

    duration_h = duration_s / 3600.0
    net_energy_wh = None
    if idle_power_w is not None and isfinite(float(idle_power_w)):
        net_energy_wh = max(0.0, energy_wh - float(idle_power_w) * duration_h)

    result.update({
        "valid": True,
        "measurement_status": "ok",
        "energy_wh": energy_wh,
        "net_energy_wh": net_energy_wh,
        "avg_power_w": energy_wh / duration_h,
        "max_power_w": max(float(p) for _, p in samples),
        "min_power_w": min(float(p) for _, p in samples),
    })
    return result


def aggregate_energy_results(results):
    """Publish a total only when every requested device has valid observations."""
    valid_count = sum(bool(item["valid"]) for item in results)
    valid = bool(results) and valid_count == len(results)
    return {
        "valid": valid,
        "measurement_status": "ok" if valid else "incomplete_device_measurements",
        "power_node_count": len(results),
        "valid_power_node_count": valid_count,
        "invalid_power_node_count": len(results) - valid_count,
        "total_energy_wh": sum(item["energy_wh"] for item in results) if valid else None,
        "total_net_energy_wh": (
            sum(item["net_energy_wh"] for item in results)
            if valid and all(item["net_energy_wh"] is not None for item in results)
            else None
        ),
    }
