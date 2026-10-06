import importlib

# 情報源モジュールの明示的な一覧。1 行 1 モジュールで追加する (sources/<name>.py の <name>)。
MODULES: tuple[str, ...] = (
    "billing_hetzner",
    "billing_hetzner_os",
    "billing_cloudflare",
    "billing_github",
    "billing_hcp_terraform",
    "billing_infisical",
    "billing_tailscale",
    "billing_netdata",
    "monitor_uptimerobot",
    "monitor_healthchecks",
    "node_resources",
    "node_maintenance",
    "cluster_workloads",
    "cluster_data_protection",
    "connect_tunnel",
    "connect_tailscale",
    "ci_github",
    "auth_zitadel",
    "security_falco",
    "mail",
)


def load_sources(modules=None):
    out, seen = [], set()
    for name in MODULES if modules is None else modules:
        for s in importlib.import_module(f"sources.{name}").SOURCES:
            if s.source_id in seen:
                raise ValueError(f"duplicate source_id: {s.source_id}")
            seen.add(s.source_id)
            out.append(s)
    return out
