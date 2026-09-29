from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

import httpx

from backend.app.ingestion.base import ReadingSample
from simulator.profiles import describe_simulator, default_profiles
from simulator.simulator import build_plan, live_sample

DEFAULT_URL = "http://127.0.0.1:8000"
BATCH_SIZE = 2000


def _payload(sample: ReadingSample) -> dict:
    return sample.as_dict()


def post_samples(
    client: httpx.Client,
    base_url: str,
    samples: list[ReadingSample],
    *,
    source: str = "simulator",
    label: str = "",
) -> dict:
    totals = {"received": 0, "inserted": 0, "updated": 0, "duplicates": 0, "rejected": 0, "errors": []}
    for start in range(0, len(samples), BATCH_SIZE):
        batch = samples[start : start + BATCH_SIZE]
        response = client.post(
            f"{base_url}/api/readings",
            json={"readings": [_payload(item) for item in batch], "source": source},
            timeout=60.0,
        )
        response.raise_for_status()
        body = response.json()
        for key in ("received", "inserted", "updated", "duplicates", "rejected"):
            totals[key] += int(body.get(key, 0))
        totals["errors"].extend(body.get("errors", []))
        if label:
            print(f"  {label}: {min(start + BATCH_SIZE, len(samples))}/{len(samples)} readings sent")
    totals["errors"] = totals["errors"][:10]
    return totals


def command_seed(args: argparse.Namespace) -> int:
    plan = build_plan(
        days=args.days,
        seed=args.seed,
        inject_anomalies=not args.no_anomalies,
        inject_gap=not args.no_gap,
    )
    samples = plan.samples()
    description = describe_simulator(plan.profiles, plan.anomalies)

    print("WattXpense simulator - seeding SIMULATED readings")
    print(f"  window:   {plan.start.isoformat()} to {plan.end.isoformat()} ({args.days} days)")
    print(f"  readings: {len(samples)}")
    for device in description["devices"]:
        role = "primary (billed)" if device["is_primary"] else "sub-circuit (not billed)"
        print(f"    - {device['device_id']}: {device['name']} every {device['interval_minutes']} min, {role}")
    if plan.anomalies:
        print("  deliberately injected anomalies (so the detector has something to find):")
        for anomaly in plan.anomalies:
            print(f"    - {anomaly.day}: {anomaly.description}")
    if plan.gaps:
        print(f"  deliberate data gaps: {', '.join(plan.gaps)}")

    if args.direct:
        return _seed_direct(samples, plan, args)
    return _seed_http(samples, plan, args, description)


def _seed_direct(samples: list[ReadingSample], plan, args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from backend.app.database import SessionLocal, init_db
    from backend.app.ingestion.service import ensure_device, persist_samples
    from backend.app.models import Device

    init_db()
    db = SessionLocal()
    try:
        for profile in plan.profiles:
            ensure_device(
                db,
                profile.device_id,
                source="simulator",
                is_simulated=True,
                name=profile.name,
                location=profile.location,
            )
        primary_ids = {p.device_id for p in plan.profiles if p.is_primary}
        for device in db.execute(select(Device)).scalars():
            device.is_primary = device.id in primary_ids
        db.commit()

        result = persist_samples(db, samples, source="simulator")
        print(f"  inserted {result.inserted}, updated {result.updated}, rejected {result.rejected}")
        for error in result.errors[:5]:
            print(f"  ! {error}")
    finally:
        db.close()
    return 0


def _seed_http(samples: list[ReadingSample], plan, args: argparse.Namespace, description: dict) -> int:
    base_url = args.url.rstrip("/")
    try:
        client = httpx.Client()
        health = client.get(f"{base_url}/api/health", timeout=5.0)
        health.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not reach the WattXpense API at {base_url}: {exc}")
        print("Start it first with:  uvicorn backend.app.main:app --reload")
        print("Or seed the database directly with:  python -m simulator.cli seed --direct")
        return 1

    try:
        if args.reset:
            for profile in plan.profiles:
                client.delete(f"{base_url}/api/readings", params={"device_id": profile.device_id}, timeout=30.0)
            print("  cleared existing readings")

        for profile in plan.profiles:
            client.post(
                f"{base_url}/api/devices",
                json={
                    "id": profile.device_id,
                    "name": profile.name,
                    "location": profile.location,
                    "source": "simulator",
                    "is_simulated": True,
                    "is_primary": profile.is_primary,
                    "nominal_voltage_v": profile.nominal_voltage_v,
                    "notes": profile.notes,
                },
                timeout=15.0,
            )

        totals = post_samples(client, base_url, samples, source="simulator", label="seeding")
        print(
            f"  done: {totals['inserted']} inserted, {totals['updated']} updated, "
            f"{totals['duplicates']} unchanged, {totals['rejected']} rejected"
        )
        for error in totals["errors"]:
            print(f"  ! {error}")
    finally:
        client.close()

    print(f"Open {base_url} and the dashboard will show this simulated history.")
    return 0


def command_live(args: argparse.Namespace) -> int:
    base_url = args.url.rstrip("/")
    profiles = default_profiles()
    print(f"WattXpense simulator - streaming live SIMULATED readings to {base_url} every {args.interval}s")
    print("Press Ctrl+C to stop.")
    client = httpx.Client()
    tick = 0
    try:
        while True:
            now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            samples = [
                live_sample(profile, now=now, seed=1000 + tick * 31 + index)
                for index, profile in enumerate(profiles)
                if not args.device_id or profile.device_id == args.device_id
            ]
            if samples:
                result = post_samples(client, base_url, samples, source="simulator")
                stamp = now.strftime("%H:%M:%S")
                detail = ", ".join(f"{s.device_id} {s.power_w:.0f} W" for s in samples)
                print(f"  {stamp} -> {detail} (inserted {result['inserted']}, updated {result['updated']})")
            tick += 1
            time.sleep(max(args.interval, 1))
    except KeyboardInterrupt:
        print("\nSimulator stopped.")
    except Exception as exc:  # noqa: BLE001
        print(f"Simulator stopped: {exc}")
        return 1
    finally:
        client.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wattxpense-simulate",
        description="Generate realistic simulated energy readings for WattXpense.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="Backfill simulated history through the ingestion API.")
    seed.add_argument("--days", type=int, default=35, help="Days of history to generate (default 35).")
    seed.add_argument("--seed", type=int, default=20260926, help="Random seed for reproducible data.")
    seed.add_argument("--url", default=DEFAULT_URL, help=f"API base URL (default {DEFAULT_URL}).")
    seed.add_argument("--no-anomalies", action="store_true", help="Do not inject the deliberate usage spikes.")
    seed.add_argument("--no-gap", action="store_true", help="Do not inject the deliberate missing-data day.")
    seed.add_argument("--reset", action="store_true", help="Delete existing readings for the simulated devices first.")
    seed.add_argument("--direct", action="store_true", help="Write straight to the database instead of using HTTP.")
    seed.set_defaults(func=command_seed)

    live = sub.add_parser("live", help="Stream readings in real time through the ingestion API.")
    live.add_argument("--interval", type=int, default=10, help="Seconds between readings (default 10).")
    live.add_argument("--url", default=DEFAULT_URL, help=f"API base URL (default {DEFAULT_URL}).")
    live.add_argument("--device-id", default=None, help="Only stream one device.")
    live.set_defaults(func=command_live)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


def seed_main() -> int:
    return main(["seed", *sys.argv[1:]])


def simulate_main() -> int:
    return main(["live", *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
