from __future__ import annotations

import argparse
from pathlib import Path

from memory_bench_platform.locomo_test_runtime_bridge import (
    bootstrap_locomo_openclaw_runtime,
)


def bootstrap_runtime(
    *,
    base_state_dir: Path,
    base_ov_conf: Path,
    state_dir: Path,
    home_dir: Path,
    config_path: Path,
    env_path: Path,
    gateway_port: int,
    run_id: str,
    runtime_config_src: Path,
    runtime_config_dst: Path,
    output_dir: str,
) -> None:
    bootstrap_locomo_openclaw_runtime(
        base_state_dir=base_state_dir,
        base_ov_conf=base_ov_conf,
        state_dir=state_dir,
        home_dir=home_dir,
        config_path=config_path,
        env_path=env_path,
        gateway_port=gateway_port,
        run_id=run_id,
        runtime_config_src=runtime_config_src,
        runtime_config_dst=runtime_config_dst,
        output_dir=output_dir,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-state-dir", required=True)
    parser.add_argument("--base-ov-conf", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--home-dir", required=True)
    parser.add_argument("--config-path", required=True)
    parser.add_argument("--env-path", required=True)
    parser.add_argument("--gateway-port", type=int, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runtime-config-src", required=True)
    parser.add_argument("--runtime-config-dst", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    bootstrap_runtime(
        base_state_dir=Path(args.base_state_dir),
        base_ov_conf=Path(args.base_ov_conf),
        state_dir=Path(args.state_dir),
        home_dir=Path(args.home_dir),
        config_path=Path(args.config_path),
        env_path=Path(args.env_path),
        gateway_port=args.gateway_port,
        run_id=args.run_id,
        runtime_config_src=Path(args.runtime_config_src),
        runtime_config_dst=Path(args.runtime_config_dst),
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
