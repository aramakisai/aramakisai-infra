import yaml
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFESTS = REPO_ROOT / "gitops" / "manifests"

ENVS = {
    "prod": {
        "deploy_path": MANIFESTS / "prod" / "directus" / "deployment.yaml",
        "secret_path": MANIFESTS / "prod" / "directus" / "external-secret.yaml",
        "secret_name": "directus-secrets",
        "sentry_env": "prod",
        "sentry_dsn_key": "DIRECTUS_PROD_SENTRY_DSN",
    },
    "staging": {
        "deploy_path": MANIFESTS / "staging" / "directus" / "deployment.yaml",
        "secret_path": MANIFESTS / "staging" / "directus" / "external-secret.yaml",
        "secret_name": "directus-staging-secrets",
        "sentry_env": "staging",
        "sentry_dsn_key": "DIRECTUS_STAGING_SENTRY_DSN",
    },
}

def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())

def load_yaml_all(path: Path) -> list[dict]:
    return list(yaml.safe_load_all(path.read_text()))

@pytest.mark.parametrize("env", ["prod", "staging"])
def test_deployment_sentry_env_vars(env):
    deploy = load_yaml(ENVS[env]["deploy_path"])
    container = deploy["spec"]["template"]["spec"]["containers"][0]
    envs = {e["name"]: e for e in container.get("env", [])}

    assert "SENTRY_DSN" in envs
    assert envs["SENTRY_DSN"]["valueFrom"]["secretKeyRef"]["name"] == ENVS[env]["secret_name"]
    assert envs["SENTRY_DSN"]["valueFrom"]["secretKeyRef"]["key"] == "SENTRY_DSN"

    assert "SENTRY_ENVIRONMENT" in envs
    assert envs["SENTRY_ENVIRONMENT"]["value"] == ENVS[env]["sentry_env"]

    assert "SENTRY_TRACES_SAMPLE_RATE" in envs
    assert envs["SENTRY_TRACES_SAMPLE_RATE"]["value"] == "0"

@pytest.mark.parametrize("env", ["prod", "staging"])
def test_deployment_sentry_volumes(env):
    deploy = load_yaml(ENVS[env]["deploy_path"])
    template_spec = deploy["spec"]["template"]["spec"]

    volumes = {v["name"]: v for v in template_spec.get("volumes", [])}
    assert "hooks-sentry-error-tracking" in volumes
    assert volumes["hooks-sentry-error-tracking"]["configMap"]["name"] == "directus-hooks-sentry-error-tracking"
    assert volumes["hooks-sentry-error-tracking"]["configMap"]["optional"] is True

@pytest.mark.parametrize("env", ["prod", "staging"])
def test_deployment_sentry_volume_mounts(env):
    deploy = load_yaml(ENVS[env]["deploy_path"])
    container = deploy["spec"]["template"]["spec"]["containers"][0]

    mounts = [m for m in container.get("volumeMounts", []) if m["name"] == "hooks-sentry-error-tracking"]
    assert len(mounts) == 2

    subpaths = {m["subPath"] for m in mounts}
    assert subpaths == {"package.json", "index.js"}

    mount_paths = {m["mountPath"] for m in mounts}
    assert mount_paths == {
        "/directus/extensions/hooks/sentry-error-tracking/package.json",
        "/directus/extensions/hooks/sentry-error-tracking/dist/index.js",
    }
    assert all(m.get("readOnly") is True for m in mounts)

@pytest.mark.parametrize("env", ["prod", "staging"])
def test_external_secret_sentry_dsn(env):
    docs = load_yaml_all(ENVS[env]["secret_path"])
    # Find the target ExternalSecret
    secret_doc = next((d for d in docs if d and d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == ENVS[env]["secret_name"]), None)
    assert secret_doc is not None

    data_list = secret_doc["spec"]["data"]
    sentry_dsn_data = next((item for item in data_list if item["secretKey"] == "SENTRY_DSN"), None)
    assert sentry_dsn_data is not None
    assert sentry_dsn_data["remoteRef"]["key"] == ENVS[env]["sentry_dsn_key"]
