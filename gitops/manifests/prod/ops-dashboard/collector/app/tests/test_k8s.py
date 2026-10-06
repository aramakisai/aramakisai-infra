import os
import tempfile
import unittest

import k8s
from helpers import FakeHttp


class K8sTest(unittest.TestCase):
    def test_get_uses_token_and_base(self):
        with tempfile.TemporaryDirectory() as d:
            tok = os.path.join(d, "token")
            with open(tok, "w") as f:
                f.write("TOK\n")
            http = FakeHttp({"https://10.0.0.1:443/api/v1/nodes": {"items": [1]}})
            c = k8s.K8sClient(http, host="10.0.0.1", port="443", token_path=tok)
            self.assertEqual(c.get("/api/v1/nodes"), {"items": [1]})
            self.assertEqual(http.calls[0]["bearer"], "TOK")

    def test_list_items_and_exclude(self):
        http = FakeHttp({"https://h:1/api/v1/pods": {"items": [{"metadata": {"namespace": "a", "name": "x"}},
                                                               {"metadata": {"namespace": "b", "name": "y"}}]}})
        c = k8s.K8sClient(http, host="h", port="1", token_path="/nonexistent")
        c._token = lambda: "T"
        items = c.list_items("/api/v1/pods", exclude=("a",))
        self.assertEqual([i["metadata"]["name"] for i in items], ["y"])

    def test_missing_env(self):
        with self.assertRaises(k8s.K8sError):
            k8s.K8sClient.in_cluster(http=None, env={})


if __name__ == "__main__":
    unittest.main()
