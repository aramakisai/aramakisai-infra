# ============================================================
# Dovecot Lua Auth Bridge用 Zitadel machine user (task 3.5、最小スコープ実機検証済み)
# ============================================================
#
# k3d実機検証の結果:
#   - CreateSession/SetSession(zitadel.session.v2.SessionService)はauth_optionが
#     permission: "authenticated" のみだが、org-levelメンバーシップのみでは
#     "membership not found (AUTHZ-cdgFk)"、instance-level read-onlyロール
#     (IAM_OWNER_VIEWER)を足しても"No matching permissions found (AUTH-AWfge)"で
#     拒否される。CreateSessionはリクエストにorg_idを含まないinstanceスコープのAPIの
#     ため、caller側もinstance-levelの専用ロールが必要。
#   - Zitadel組み込みのbuiltin role "IAM_LOGIN_CLIENT"
#     (login v2 UI自身がFirstInstanceで自動発行されるのと同じロール、session.write/
#     session.read/user.grant.read等を含む)が実際に成功した最小ロール。
#     IAM_OWNER(iam.write, instance policy write等の全権)は不要。
resource "zitadel_machine_user" "dovecot_lua_auth" {
  org_id      = var.zitadel_org_id
  user_name   = "dovecot-lua-auth"
  name        = "Dovecot Lua Auth Bridge"
  description = "Session API(認証委譲) + Management API user grant read only"
  with_secret = false
}

resource "zitadel_instance_member" "dovecot_lua_auth" {
  user_id = zitadel_machine_user.dovecot_lua_auth.id
  roles   = ["IAM_LOGIN_CLIENT"]
}

resource "zitadel_personal_access_token" "dovecot_lua_auth" {
  org_id          = var.zitadel_org_id
  user_id         = zitadel_machine_user.dovecot_lua_auth.id
  expiration_date = "2029-01-01T00:00:00Z"
}

# 発行したPATはInfisicalへ手動登録する(DOVECOT_ZITADEL_AUTH_PAT、
# gitops/manifests/prod/mailserver/dovecot-lua-auth-external-secret.yaml参照)。
# infisical-authと同型の例外ブートストラップのため、terraform outputには含めない。
