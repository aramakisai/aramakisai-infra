"""Structural checks for the Directus schema-apply PostSync Job manifests.

Covers cicd-pipeline spec tasks 5.1 / 5.2 (prod + staging).
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFESTS = REPO_ROOT / "gitops" / "manifests"

ENVS = {
    "prod": {
        "job_path": MANIFESTS / "prod" / "directus" / "schema-apply-job.yaml",
        "app_path": REPO_ROOT / "gitops" / "apps" / "prod" / "directus.yaml",
        "namespace": "prod",
        "secret_name": "directus-secrets",
    },
    "staging": {
        "job_path": MANIFESTS / "staging" / "directus" / "schema-apply-job.yaml",
        "app_path": REPO_ROOT / "gitops" / "apps" / "staging" / "directus.yaml",
        "namespace": "staging",
        "secret_name": "directus-staging-secrets",
    },
}


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


@pytest.mark.parametrize("env", ["prod", "staging"])
def test_job_has_backoff_limit_one(env):
    job = load_yaml(ENVS[env]["job_path"])
    assert job["spec"]["backoffLimit"] == 1


@pytest.mark.parametrize("env", ["prod", "staging"])
def test_job_restart_policy_on_failure(env):
    job = load_yaml(ENVS[env]["job_path"])
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "OnFailure"


@pytest.mark.parametrize("env", ["prod", "staging"])
def test_job_is_postsync_hook_with_cleanup(env):
    job = load_yaml(ENVS[env]["job_path"])
    annotations = job["metadata"]["annotations"]
    assert annotations["argocd.argoproj.io/hook"] == "PostSync"
    assert annotations["argocd.argoproj.io/hook-delete-policy"] == "HookSucceeded"
    assert job["spec"]["ttlSecondsAfterFinished"] == 3600


@pytest.mark.parametrize("env", ["prod", "staging"])
def test_job_namespace_and_secret(env):
    job = load_yaml(ENVS[env]["job_path"])
    assert job["metadata"]["namespace"] == ENVS[env]["namespace"]
    container = job["spec"]["template"]["spec"]["containers"][0]
    secret_refs = [ref["secretRef"]["name"] for ref in container["envFrom"]]
    assert ENVS[env]["secret_name"] in secret_refs


@pytest.mark.parametrize("env", ["prod", "staging"])
def test_job_mounts_schema_and_optional_migrations_configmap(env):
    job = load_yaml(ENVS[env]["job_path"])
    volumes = {v["name"]: v for v in job["spec"]["template"]["spec"]["volumes"]}
    assert volumes["snapshot"]["configMap"]["name"] == "directus-schema"
    assert volumes["migrations"]["configMap"]["name"] == "directus-migrations"
    assert volumes["migrations"]["configMap"]["optional"] is True


@pytest.mark.parametrize("env", ["prod", "staging"])
def test_argocd_app_has_self_heal(env):
    app = load_yaml(ENVS[env]["app_path"])
    assert app["spec"]["syncPolicy"]["automated"]["selfHeal"] is True
