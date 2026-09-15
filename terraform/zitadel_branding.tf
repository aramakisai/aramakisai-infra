# ============================================================
# Zitadel Label Policy (ブランディング設定, task10.7)
# ============================================================
#
# 移行元authentik_brand.tf(authentikデフォルトの未カスタマイズ状態)の後継。
# terraform-provider-zitadel v3系のzitadel_label_policyはlogo_path/icon_path/
# font_path(+それぞれの*_hash)でローカルファイルパスを直接指定するとプロバイダが
# multipart uploadまで行う(2026-09-01、registry docs
# `logo_hash = filemd5("/path/to/logo.jpg")` / `logo_path = "/path/to/logo.jpg"`の
# 例で確認)ため、Admin/Management APIへの補完スクリプトは不要だった。
#
# ロゴ/faviconはtask10.6で配置済みの原本(リネーム・変形なし)をそのまま参照する
# (gitops/manifests/prod/zitadel/branding/)。favicon(icon_path/icon_dark_path)は
# Requirement 17.2通りlight/dark共通で同一ファイルを指定する。
#
# Requirement 17.3の「Google Fonts『LINE Seed JP』」カスタムフォント適用は対象外とした。
# k3d実機検証(2026-09-15)でZitadelのfont upload APIが512KB上限を持つことが判明し
# (未達時のエラーメッセージが上限超過を示さずGoの一時ファイルI/Oエラーをそのまま返す
# ため原因特定に手間取った)、LINE Seed JP Regular ttf原本(約3.67MB、CJKフルカバー)は
# 全く収まらない。文字種をASCII+かな+全角記号(591字)まで絞れば207KBに収まり実際に
# アップロード・反映まで確認できたが、この場合本文中の漢字はカスタムフォント非適用
# (システムフォントへフォールバック)になる。この制約を踏まえフォント適用自体を
# 見送る判断とした。
locals {
  branding_dir = "${path.module}/../gitops/manifests/prod/zitadel/branding"
  logo_light   = "${local.branding_dir}/aramakisai.png"
  logo_dark    = "${local.branding_dir}/aramakisai_W.png"
  favicon      = "${local.branding_dir}/favicon.png"
}

resource "zitadel_label_policy" "aramakisai" {
  org_id = var.zitadel_org_id

  # Requirement 17.4: 荒牧祭公式サイトのカラートークン
  primary_color    = "#ebb03c"
  background_color = "#ffffff"
  warn_color       = "#e86f30"
  font_color       = "#231815"

  primary_color_dark    = "#ebb03c"
  background_color_dark = "#231815"
  warn_color_dark       = "#e86f30"
  font_color_dark       = "#ffffff"

  # Requirement 17.6: テーマモードauto(OS/ブラウザ設定に追従)
  theme_mode = "THEME_MODE_AUTO"

  # Requirement 17.5: "Powered by ZITADEL"ウォーターマーク非表示
  disable_watermark = true

  # Requirement 17.7: ログイン名をuser@domainのフル形式表示
  # (hide_login_name_suffixは"urn:zitadel:iam:org:domain:primary:{domainname}"スコープが
  # リクエストされた場合のみ作用するオプトインの抑制機能であり、本移行のRPアプリはこの
  # スコープを要求しないため実質的にsuffixは常時表示されるが、意図を明示するためfalseを
  # 明示的に設定する)
  hide_login_name_suffix = false

  # Requirement 17.1/17.8: ロゴ(light: カラー版 / dark: 白版、変形・色変更・書体変更・
  # 装飾なしの原本をそのまま指定)
  #
  # k3d実機検証(2026-09-15)で、login v2 UI(StatefulSetのloginコンテナ)は
  # ここでアップロードしたアセットを自身のオリジンからの相対パス(/assets/v1/...)で
  # 取得しようとするため、login UIとZitadel API(assetの実体)が別オリジン(別ポート)
  # のままだと404になりログイン画面にロゴ/iconが表示されないことを確認した
  # (アセット自体はAPI側で md5 一致確認済み、正しくアップロードされている)。
  # 本番はcloudflared等のリバースプロキシで/assetsパスをAPI側Serviceへ、それ以外を
  # login側Serviceへ振り分ける単一オリジン構成が必須。現状の
  # gitops/manifests/prod/zitadel/にはその振り分けを行うIngress/Route定義が
  # 存在しないため、task9(本番カットオーバー)着手前に追加が必要。
  logo_path      = local.logo_light
  logo_hash      = filemd5(local.logo_light)
  logo_dark_path = local.logo_dark
  logo_dark_hash = filemd5(local.logo_dark)

  # Requirement 17.2: favicon(light/dark共通、aramakisai-web既存アイコンを流用)
  icon_path      = local.favicon
  icon_hash      = filemd5(local.favicon)
  icon_dark_path = local.favicon
  icon_dark_hash = filemd5(local.favicon)

  # 作成直後にactiveへ反映する(draftのままではログイン画面へ反映されない)
  set_active = true
}
