"""Breeder — telemetry-only workload-niche inference (ADR-0099).

This package MUST NOT import any Temper client. The breeding agent infers
workload niches from Datadog telemetry alone; reading the workload spec out of
Temper is Cedar-forbidden for the breeder agent_type. Keeping this package
Temper-free is the code-side half of that firewall (Cedar is the enforced half).
"""
