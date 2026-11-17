import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("review_isolation", Path(__file__).with_name("reviewer-isolation.py"))
isolation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(isolation)


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(Path(__file__).with_name("reviewer-isolation.example.json").read_text())

    def document(self):
        chain = {"family": "bridge", "table": "agent_beta", "name": "forward", "type": "filter",
                 "hook": "forward", "prio": -200, "policy": "accept"}
        return {"nftables": [{"chain": chain}, *[{"rule": {
            "family": "bridge", "table": "agent_beta", "chain": "forward", "expr": expression}}
            for expression in isolation.expected_bridge_expressions(self.config)]]}

    def test_only_one_local_address_and_exact_port_are_admitted(self):
        for target in [["1.1.1.1"], ["192.168.1.10", "192.168.1.11"], ["169.254.169.254"]]:
            with tempfile.TemporaryDirectory() as folder:
                value = dict(self.config, upstream_ipv4=target)
                path = Path(folder) / "config.json"
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    isolation.load(path)
        _, forward = isolation.rules(self.config)
        self.assertIn("-s 172.29.244.18/32 -d 192.168.1.10/32 -p tcp --dport 8000 -j RETURN", forward)
        self.assertEqual(forward[-1], "-j DROP")
        self.assertFalse(any("--dport 443" in row for row in forward))

    def test_unexpected_bridge_accept_rule_is_refused(self):
        document = self.document()
        isolation.verify_bridge(self.config, document)
        document["nftables"].append({"rule": {"family": "bridge", "table": "agent_beta", "chain": "forward", "expr": [{"accept": None}]}})
        with self.assertRaises(RuntimeError):
            isolation.verify_bridge(self.config, document)

    def test_changed_destination_or_hook_is_refused(self):
        original = self.document()
        document = copy.deepcopy(original)
        document["nftables"][2]["rule"]["expr"][2]["match"]["right"] = "192.168.1.99"
        with self.assertRaises(RuntimeError):
            isolation.verify_bridge(self.config, document)
        document = copy.deepcopy(original)
        document["nftables"][0]["chain"]["prio"] = 0
        with self.assertRaises(RuntimeError):
            isolation.verify_bridge(self.config, document)


if __name__ == "__main__":
    unittest.main()
