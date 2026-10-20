"""Small host-side guards shared by offline acceptance drivers."""

import os
import subprocess


def command(arguments, strip=True, **kwargs):
    output = subprocess.run(arguments, check=True, capture_output=True, text=True,
        timeout=kwargs.pop("timeout", 60), **kwargs).stdout
    return output.strip() if strip else output


def local_docker():
    endpoint = os.environ.get("DOCKER_HOST") or command(
        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"])
    if not endpoint.startswith("unix://"):
        raise ValueError("Only a local Unix Docker endpoint is allowed")


def remove_owned(containers, token):
    """Attempt every owned cleanup, even if an earlier removal failed."""
    result = {"removed": [], "errors": []}
    for container in reversed(containers):
        try:
            owner = command(["docker", "inspect", "--format",
                '{{index .Config.Labels "io.qavector.acceptance"}}', container])
            if owner != token:
                raise RuntimeError("Refusing cleanup of a container not owned by this acceptance")
            command(["docker", "rm", "--force", container])
            result["removed"].append(container)
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            result["errors"].append({"container": container, "error": str(error)})
    return result


def run_attached(container, log, timeout):
    try:
        result = subprocess.run(["docker", "start", "--attach", container],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        # TimeoutExpired may carry bytes even when text=True. Retain evidence
        # before the caller removes its own container in finally.
        def decode(value):
            return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
        log.write_text(decode(error.stdout) + decode(error.stderr) + "\nAcceptance timed out\n")
        raise
    log.write_text(result.stdout + result.stderr)
    return result
