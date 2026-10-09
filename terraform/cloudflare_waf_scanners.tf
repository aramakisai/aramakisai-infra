# apex (aramakisai.com) への脆弱性スキャナを Worker 起動前に遮断する WAF カスタムルール。
# Worker は未知パスに 404 を返すだけで、起動回数と CPU 時間を消費する。
#
# expression は API 側で正規化されて恒常 diff にならないよう、heredoc を 1 行に畳んでいる。
#
# zone あたり http_request_firewall_custom フェーズの entrypoint ruleset は 1 つのみ。
# Free プランはカスタムルール 5 件までなので、パターンは 1 ルールに or で束ねて枠を節約する。
# 他ホスト (cms の /api/graphql や webmail の .php など) は正規利用があり得るため、
# 必ず http.host で apex に限定する。
#
# 旧 WordPress サイトの URL (/2025/*) は Worker が 301 で新 URL へ転送する正規経路なので、
# /2025 配下には一切触れない。/wp-* は旧サイトの正規ページが無く、リダイレクト対象にもなっていない。
resource "cloudflare_ruleset" "waf_block_scanners" {
  zone_id = var.cloudflare_zone_id
  name    = "default" # zone phase entrypoint の name は固定
  kind    = "zone"
  phase   = "http_request_firewall_custom"

  rules {
    description = "apex への脆弱性スキャナ (.php/.env/.git/wp-*/xmlrpc/graphql 等) を遮断"
    action      = "block"
    enabled     = true
    expression = replace(trimspace(<<-EOT
      (http.host eq "aramakisai.com") and (
        lower(http.request.uri.path.extension) in {"php" "asp" "aspx" "jsp" "sql" "env" "pem" "key"}
        or (http.request.uri.path contains "/." and not starts_with(http.request.uri.path, "/.well-known/"))
        or starts_with(http.request.uri.path, "/wp-")
        or starts_with(http.request.uri.path, "/wordpress")
        or starts_with(http.request.uri.path, "/xmlrpc")
        or starts_with(http.request.uri.path, "/@fs/")
        or starts_with(http.request.uri.path, "/cgi-bin/")
        or starts_with(http.request.uri.path, "/phpmyadmin")
        or http.request.uri.path in {"/api/graphql" "/graphql" "/server-status"}
      )
    EOT
    ), "/\\s+/", " ")
  }
}
