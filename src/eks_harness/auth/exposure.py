from __future__ import annotations

import ipaddress

from eks_harness.config import Config


class ExposureError(RuntimeError):
    pass


def is_loopback_host(host: str) -> bool:
    host = (host or "").strip()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def exposure_problem(host: str, auth_enabled: bool) -> str | None:
    if auth_enabled or is_loopback_host(host):
        return None
    return (f"Listening on {host} with authentication disabled would let anyone on the network use the harness. "
            f"Enable auth (auth.enabled true, then 'eks-harness setup' or 'eks-harness auth recover' for an admin "
            f"key), bind to 127.0.0.1, or pass --force to accept the risk.")


def config_exposure_problem(config: Config) -> str | None:
    return exposure_problem(config.listen_host(), bool(config["auth.enabled"]))


def ensure_safe_bind(config: Config, force: bool = False) -> None:
    problem = config_exposure_problem(config)
    if problem and not force:
        raise ExposureError(problem)
