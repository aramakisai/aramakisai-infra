import os
import ssl
from typing import Mapping

from http_client import Http

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class K8sError(Exception):
    pass


class K8sClient:
    def __init__(self, http, host: str, port: str, token_path: str = f"{SA_DIR}/token"):
        self.http = http
        self.base = f"https://{host}:{port}"
        self.token_path = token_path

    @classmethod
    def in_cluster(cls, http=None, env: Mapping[str, str] | None = None) -> "K8sClient":
        env = os.environ if env is None else env
        host, port = env.get("KUBERNETES_SERVICE_HOST"), env.get("KUBERNETES_SERVICE_PORT")
        if not host or not port:
            raise K8sError("not running in a cluster")
        if http is None:
            http = Http(ssl_context=ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt"))
        return cls(http, host, port)

    # ServiceAccount トークンは kubelet が定期的に差し替えるため、毎回読み直す。
    def _token(self) -> str:
        with open(self.token_path) as f:
            return f.read().strip()

    def get(self, path: str, params: Mapping[str, str] | None = None):
        return self.http.get_json(self.base + path, bearer=self._token(), params=params)

    def list_items(self, path: str, exclude=()) -> list[dict]:
        """items を返す。metadata.namespace または metadata.name が exclude に含まれるものは除く。"""
        return [i for i in self.get(path).get("items", [])
                if i["metadata"].get("namespace") not in exclude and i["metadata"].get("name") not in exclude]

    def kubelet_stats(self, node_ip: str):
        # k3s の kubelet 提供証明書は API サーバーと同じ server CA の署名で SAN に InternalIP を含むため、
        # ServiceAccount の ca.crt で検証できる
        return self.http.get_json(f"https://{node_ip}:10250/stats/summary", bearer=self._token())

    def kubelet_text(self, node_ip: str, path: str) -> str:
        """kubelet の Prometheus テキスト (/metrics/cadvisor 等)。権限は nodes/metrics。"""
        return self.http.get_bytes(f"https://{node_ip}:10250{path}", bearer=self._token()).decode("utf-8", "replace")
