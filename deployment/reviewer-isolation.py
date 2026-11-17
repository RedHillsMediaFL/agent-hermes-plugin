#!/usr/bin/env python3
"""Publisher-only, narrowly scoped reviewer network policy. Run as root.

Only AGENT_BETA chains and the bridge/agent_beta nft table are owned here.
Other Docker networks and host traffic retain their existing policy.
"""
import argparse
import ipaddress
import json
from pathlib import Path
import subprocess

INPUT = "AGENT_BETA_IN"
FORWARD = "AGENT_BETA_FWD"


def run(args, *, data=None, check=True):
    return subprocess.run(args, input=data, text=True, capture_output=True,
                          timeout=15, check=check)


def load(path):
    value = json.loads(Path(path).read_text())
    for name in ["internal_subnet", "egress_subnet"]:
        network = ipaddress.ip_network(value[name])
        if network.version != 4 or not network.is_private:
            raise ValueError("Expected a dedicated private IPv4 subnet")
    for name in ["hermes_ip", "provider_internal_ip", "provider_egress_ip", "host_gateway"]:
        if ipaddress.ip_address(value[name]).version != 4:
            raise ValueError("Expected an IPv4 container address")
    if not value["upstream_ipv4"] or any(not ipaddress.ip_address(item).is_global for item in value["upstream_ipv4"]):
        raise ValueError("Provider egress must name explicit public IPv4 addresses")
    if value["internal_bridge"] != "br-agent-beta-i" or not 1024 <= value["provider_port"] <= 65535:
        raise ValueError("Unexpected reviewer bridge/port")
    return value


def rules(value):
    inbound = ["-m conntrack --ctstate RELATED,ESTABLISHED -j RETURN", "-j DROP"]
    forwarded = ["-m conntrack --ctstate RELATED,ESTABLISHED -j RETURN",
        f"-s {value['host_gateway']}/32 -d {value['hermes_ip']}/32 -p tcp --dport 9119 -j RETURN",
        f"-s {value['hermes_ip']}/32 -d {value['provider_internal_ip']}/32 -p tcp --dport {value['provider_port']} -j RETURN"]
    for address in value["upstream_ipv4"]:
        forwarded.append(f"-s {value['provider_egress_ip']}/32 -d {address}/32 -p tcp --dport 443 -j RETURN")
    forwarded.append("-j DROP")
    return inbound, forwarded


def nft_policy(value, exists):
    bridge = value["internal_bridge"]
    prefix = "delete table bridge agent_beta\n" if exists else ""
    return prefix + f"""table bridge agent_beta {{
  chain forward {{
    type filter hook forward priority -200; policy accept;
    meta ibrname \"{bridge}\" ether type arp accept
    meta ibrname \"{bridge}\" ip saddr {value['host_gateway']} ip daddr {value['hermes_ip']} tcp dport 9119 accept
    meta ibrname \"{bridge}\" ip saddr {value['hermes_ip']} ip daddr {value['host_gateway']} tcp sport 9119 accept
    meta ibrname \"{bridge}\" ip saddr {value['hermes_ip']} ip daddr {value['provider_internal_ip']} tcp dport {value['provider_port']} accept
    meta ibrname \"{bridge}\" ip saddr {value['provider_internal_ip']} ip daddr {value['hermes_ip']} tcp sport {value['provider_port']} accept
    meta ibrname \"{bridge}\" drop
  }}
}}
"""


def apply(value):
    inbound, forwarded = rules(value)
    # iptables-restore applies the complete owned chain update atomically.
    document = ["*filter", f":{INPUT} - [0:0]", f":{FORWARD} - [0:0]", f"-F {INPUT}", f"-F {FORWARD}"]
    document += [f"-A {INPUT} {line}" for line in inbound]
    document += [f"-A {FORWARD} {line}" for line in forwarded]
    document += ["COMMIT", ""]
    run(["iptables-restore", "--wait", "5", "--noflush"], data="\n".join(document))
    for subnet in [value["internal_subnet"], value["egress_subnet"]]:
        for chain, arguments in [("INPUT", ["-s", subnet, "-j", INPUT]),
                                 ("DOCKER-USER", ["-s", subnet, "-j", FORWARD]),
                                 ("DOCKER-USER", ["-d", subnet, "-j", FORWARD])]:
            if run(["iptables", "-w", "5", "-C", chain, *arguments], check=False).returncode:
                run(["iptables", "-w", "5", "-I", chain, "1", *arguments])
    exists = run(["nft", "list", "table", "bridge", "agent_beta"], check=False).returncode == 0
    policy = nft_policy(value, exists)
    run(["nft", "--check", "--file", "-"], data=policy)
    run(["nft", "--file", "-"], data=policy)


def verify(value):
    # Check every required jump and exact owned-chain rule count. An unexpected
    # extra accept rule cannot pass simply because the required denies exist.
    inbound, forwarded = rules(value)
    import shlex
    for chain, expected in [(INPUT, inbound), (FORWARD, forwarded)]:
        actual = run(["iptables", "-S", chain]).stdout.splitlines()
        if len([line for line in actual if line.startswith("-A ")]) != len(expected):
            raise RuntimeError("Unexpected reviewer rule count")
        for rule in expected:
            run(["iptables", "-w", "5", "-C", chain, *shlex.split(rule)])
    for subnet in [value["internal_subnet"], value["egress_subnet"]]:
        run(["iptables", "-w", "5", "-C", "INPUT", "-s", subnet, "-j", INPUT])
        for direction in ["-s", "-d"]:
            run(["iptables", "-w", "5", "-C", "DOCKER-USER", direction, subnet, "-j", FORWARD])
    bridge = run(["nft", "--json", "list", "table", "bridge", "agent_beta"]).stdout
    if value["internal_bridge"] not in bridge or '"drop"' not in bridge:
        raise RuntimeError("Reviewer bridge policy is missing")
    return {"ok": True, "host_input_closed": True, "hermes_only_provider_port": value["provider_port"],
            "provider_egress_ipv4": value["upstream_ipv4"], "bridge_isolation": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["apply", "check"])
    parser.add_argument("--config", default="/opt/agent-beta/isolation.json")
    arguments = parser.parse_args()
    config = load(arguments.config)
    if arguments.action == "apply":
        apply(config)
    print(json.dumps(verify(config), sort_keys=True))
