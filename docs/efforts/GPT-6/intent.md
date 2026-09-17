# Intent

Authenticated MCP reads could return 403 without generating an approval
request, leaving MCP runtimes unable to offer the human-governed consent flow.
Separately, the advertised tenant-first pinned-installation helper had been
retired, so `install_app` calls reached a metadata-only endpoint that could
not materialise a bundle, and the structured denial returned no decision ID.

The effort fixes both gaps without widening Cedar policy: it records a pending
decision at the HTTP boundary when an explicit authenticated agent request is
denied, restores the narrow adapter to the governed pinned Genesis installer,
and generates a distinct session for MCP runtimes that do not supply one — so
the approval contract is reachable from an unconfigured client.

Tracked as [ARN-467](https://linear.app/arni-build/issue/ARN-467).
