from model import Source, make_result
from plan import credential_items, jst_today, plan_item, quota_item
from render.labels import label


def render(ctx, query):
    """Infisical は使用量を取得できないため宣言値だけ。期限日は日付で状態が変わるので資格情報の期限もここで毎回評価する。"""
    now = ctx.now()
    plan = ctx.config.active_plan("infisical", jst_today(now))
    items = [plan_item(ctx.config, "infisical", now)]
    if plan:
        items.append(quota_item("quota.infisical_identities", "quota.infisical_identities", None,
                                plan.limits.get("identities"), "identities", ctx.config.thresholds.warn_ratio,
                                note=label("note.quota_declared_only")))
    return make_result("billing.infisical", now, items + credential_items(ctx.config, now))


SOURCES = (Source("billing.infisical", None, None, render=render),)
