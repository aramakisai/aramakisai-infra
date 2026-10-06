import model
from model import Item, Status
from render.components import Raw, card, esc, fmt_bytes, fmt_dt, fmt_int, fmt_ratio, status_cell, table
from render.labels import label
from render.sections import Section

# サービス名は固有名詞なので文言ではなくここに持つ。
_SERVICE = {
    "cloudflare_workers": "Cloudflare Workers", "cloudflare_r2": "Cloudflare R2",
    "cloudflare_zero_trust": "Cloudflare Zero Trust", "hcp_terraform": "HCP Terraform", "infisical": "Infisical",
    "tailscale": "Tailscale", "netdata_cloud": "Netdata Cloud", "healthchecks": "Healthchecks.io",
    "uptimerobot": "UptimeRobot", "github": "GitHub", "hetzner_object_storage": "Hetzner Object Storage",
}
_CURRENCY = {"USD": "$", "EUR": "€"}
_BILLED = ("cloudflare", "github")


def _money(v) -> str:
    return f'{_CURRENCY.get(v["currency"], "")}{v["amount"]:,.2f}'


def _num(v, unit) -> str:
    if v is None:
        return "-"
    return fmt_bytes(v * 1e9) if unit == "GB" else fmt_int(v)


def _period(v) -> str:
    if not v.get("from"):
        return "-"
    if v.get("until"):
        return label("period.range", **{"from": v["from"], "until": v["until"]})
    return label("period.open", **{"from": v["from"]})


def _plans(items, billed):
    plans = [i for i in items if i.key.startswith("plan.")]
    rows = []
    for n, i in enumerate(plans):
        v = i.values
        # 請求額はアカウント単位なので、同じ情報源の複数プランのうち先頭の行にだけ出す。
        b = (billed if n == 0 else "-") if billed else label("billed.na")
        rows.append([_SERVICE.get(v["service"], v["service"]), v["plan"] or "-", _period(v), b, status_cell(i)])
    return table([label("col.service"), label("col.plan"), label("col.period"), label("col.billed"), ""],
                 rows) if rows else ""


def _quota_row(i: Item):
    v = i.values
    unit = v.get("unit")
    if v.get("used") is None:
        # 使用量を取れない項目に正常バッジを付けると「問題なし」と読めてしまうため、注記だけにする。
        cell = Raw(f'<span class="note">{esc(i.note or "")}</span>')
        return [i.label, "-", _num(v.get("limit"), unit), "-", cell]
    ratio = v.get("ratio")
    if ratio is None and v.get("limit"):
        ratio = v["used"] / v["limit"]
    return [i.label, _num(v["used"], unit), _num(v.get("limit"), unit), fmt_ratio(ratio), status_cell(i)]


def _quotas(items):
    rows = [_quota_row(i) for i in items if i.key.startswith("quota.")]
    return table([label("col.quota"), label("col.used"), label("col.limit"), label("col.ratio"), ""],
                 rows, num_cols=(1, 2, 3)) if rows else ""


def _generic(items):
    billed = next((i for i in items if i.key.startswith("billed.")), None)
    text = label(billed.key, amount=_money(billed.values)) if billed else None
    plans = _plans(items, text)
    # プラン行がなければ請求額の置き場がないので単独で出す。
    lead = plans or (f'<p>{esc(label(billed.key, amount=_money(billed.values)))}</p>' if billed else "")
    return f"{lead}{_quotas(items)}"


def _hetzner(items):
    rows = []
    for i in (i for i in items if i.key.startswith("hetzner.server.")):
        v = i.values
        since = fmt_dt(model.parse_iso(v["created"])) if v.get("created") else "-"
        rows.append([i.label, v.get("type", "-"), v.get("location", "-"), since, status_cell(i)])
    est = next((i for i in items if i.key == "hetzner.estimate"), None)
    head = ""
    if est:
        v = est.values
        head = (f'<p>{esc(label("hetzner.estimate", amount=_money(v)))}</p>'
                f'<p>{esc(label("hetzner.traffic", used=fmt_bytes(v["outgoing_bytes"]), included=fmt_bytes(v["included_bytes"])))}</p>')
    return head + str(table([label("col.server"), label("col.type"), label("col.location"),
                             label("col.running_since"), ""], rows))


def _object_storage(items):
    total = next((i for i in items if i.key == "os.total"), None)
    out = _plans(items, None)
    if total:
        v = total.values
        out += (f'<p>{esc(label("os.total", size=_num(v["used"], "GB"), included=_num(v["limit"], "GB")))} '
                f'{status_cell(total)}</p>')
    lis = "".join(f'<li>{esc(label("os.bucket", bucket=i.label, size=fmt_bytes(i.values["bytes"])))}</li>'
                  for i in items if i.key.startswith("os.bucket."))
    return f"{out}<ul>{lis}</ul>" if lis else out


def _credentials(items):
    rows = [[i.label, i.values["expires_on"], status_cell(i)] for i in items if i.key.startswith("cred.")]
    return table([label("col.credential"), label("col.expires_on"), ""], rows)


def _monitor_quota(items):
    # monitor.* の quota 項目の label はサービス ID なので、画面表記に置き換える。
    return _quotas([Item("quota.x", label(f"quota.{i.label}"), i.status, i.values, i.note)
                    for i in items if i.key == "quota"])


def _not_cred(i):
    return not i.key.startswith("cred.")


def _is_quota(i):
    return i.key == "quota"


def render(snapshot, query):
    g = snapshot.get
    cf, gh, ts, tf, nd, inf = (g(f"billing.{k}") for k in ("cloudflare", "github", "tailscale", "hcp_terraform",
                                                           "netdata", "infisical"))
    cards = [card(label("sub.hetzner"), g("billing.hetzner"), _hetzner, source="Hetzner Cloud API"),
             card("Cloudflare", cf, _generic, select=_not_cred, source="Cloudflare API"),
             card("GitHub", gh, _generic, select=_not_cred, source="GitHub API"),
             card("HCP Terraform", tf, _generic, select=_not_cred, source="HCP Terraform API"),
             card("Infisical", inf, _generic, select=_not_cred),
             card("Tailscale", ts, _generic, select=_not_cred, source="Tailscale API"),
             card("Netdata Cloud", nd, _generic, select=_not_cred, source="Netdata Cloud API"),
             card(label("quota.uptimerobot"), g("monitor.uptimerobot"), _monitor_quota, source="UptimeRobot API",
                  select=_is_quota),
             card(label("quota.healthchecks"), g("monitor.healthchecks"), _monitor_quota,
                  source="Healthchecks.io API", select=_is_quota),
             card(label("sub.object_storage"), g("billing.hetzner_os"), _object_storage, source="Hetzner Object Storage"),
             card(label("sub.credentials"), inf, _credentials,
                  select=lambda i: i.key.startswith("cred."))]
    return "".join(map(str, cards))


SECTION = Section("billing", "nav.billing", "sec.billing",
                  ("billing.hetzner", "billing.hetzner_os", "billing.cloudflare", "billing.github",
                   "billing.hcp_terraform", "billing.infisical", "billing.tailscale", "billing.netdata"), render)
