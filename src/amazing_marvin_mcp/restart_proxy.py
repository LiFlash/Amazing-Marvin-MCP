"""Trigger a restart of a sibling Coolify application via the Coolify API.

Used as the `post_deployment_command` on the amazing-marvin-mcp app so
that the fronting `mcp-proxy` picks up the freshly-deployed backend
(SSE sessions are in-memory and die with the container, leaving
``404 Could not find session`` errors until the proxy is restarted).

Env vars required:
  COOLIFY_API_TOKEN     — API token with "deploy" scope.
  MCP_PROXY_APP_UUID    — UUID of the application to restart (the
                          mcp-proxy app).
  COOLIFY_BASE_URL      — base URL of the Coolify server. Defaults to
                          ``http://host.docker.internal:8000``, the
                          host-bridge alias from inside a container on
                          a single-node Coolify install.

Exits 0 on success, non-zero on failure. The deploy log will surface
the failure, but a missing restart-target is non-fatal (we log and
return) so the deploy itself isn't blocked.
"""

from __future__ import annotations

import logging
import os
import sys
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def restart_proxy() -> int:
    token = os.environ.get("COOLIFY_API_TOKEN", "").strip()
    target_uuid = os.environ.get("MCP_PROXY_APP_UUID", "").strip()
    base = os.environ.get(
        "COOLIFY_BASE_URL", "http://host.docker.internal:8000"
    ).rstrip("/")

    if not token or not target_uuid:
        print(
            "[restart_proxy] COOLIFY_API_TOKEN or MCP_PROXY_APP_UUID not set; "
            "skipping post-deploy restart."
        )
        return 0

    url = f"{base}/api/v1/applications/{target_uuid}/restart"
    req = urllib.request.Request(
        url,
        method="GET",  # Coolify v4 restart is GET, not POST
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            print(
                f"[restart_proxy] {resp.status} {resp.reason} from {url}: {body[:200]}"
            )
        return 0
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(
            f"[restart_proxy] HTTP {e.code} from {url}: {body[:200]}",
            file=sys.stderr,
        )
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[restart_proxy] failed to call {url}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(restart_proxy())
