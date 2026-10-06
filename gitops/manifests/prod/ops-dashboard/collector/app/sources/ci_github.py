from datetime import timedelta

import model
from model import Item, Source, Status

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
INCIDENT_LABELS = (("dr-incident", Status.CRIT), ("infra-alert", Status.WARN))


def fetch(ctx):
    s = ctx.config.source_settings("ci.github")
    org, repos = s["org"], s["repos"]
    tok = ctx.secret("OPS_GITHUB_TOKEN")
    since = (ctx.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    items = []

    def get(path, params):
        return ctx.http.get_json(f"{API}{path}", params=params, headers=HEADERS, bearer=tok)

    for repo in repos:
        runs = get(f"/repos/{org}/{repo}/actions/runs",
                   {"status": "failure", "created": f">={since}", "per_page": "100"})["workflow_runs"]
        for r in runs:
            items.append(Item(f"run.{repo}.{r['id']}", r["name"], Status.WARN, {
                "repo": repo, "failed_at": r.get("updated_at") or r["created_at"], "url": r["html_url"],
                "branch": r.get("head_branch")}))

    scope = " ".join(f"repo:{org}/{r}" for r in repos)

    def search(q):
        return get("/search/issues", {"q": f"{scope} {q}", "per_page": "100"})["items"]

    for i in search("is:pr is:open author:app/renovate"):
        items.append(Item(f"renovate.{i['number']}", i["title"], Status.OK, {
            "repo": i["repository_url"].rsplit("/", 1)[-1], "opened_at": i["created_at"], "url": i["html_url"]}))
    for label, status in INCIDENT_LABELS:
        for i in search(f"is:issue is:open label:{label}"):
            items.append(Item(f"incident.{label}.{i['number']}", i["title"], status, {
                "label": label, "repo": i["repository_url"].rsplit("/", 1)[-1], "opened_at": i["created_at"],
                "url": i["html_url"]}))
    return model.make_result("ci.github", ctx.now(), items, status=Status.OK if not items else None)


SOURCES = (Source("ci.github", timedelta(minutes=10), fetch),)
