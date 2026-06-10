# ============================================================
# authentik_imports.tf — 既存リソースのインポート定義
# ============================================================
# この定義ファイルを使用して、既存の手動設定された Provider/Application を
# 安全に IaC 管理下に取り込みます。

import {
  to = authentik_provider_oauth2.roundcube
  id = "12"
}

import {
  to = authentik_application.roundcube
  id = "roundcube"
}

import {
  to = authentik_provider_oauth2.argocd
  id = "10"
}

import {
  to = authentik_application.argocd
  id = "argocd"
}

# --- invitation-enrollment フロー ---

import {
  to = authentik_flow.invitation_enrollment
  id = "invitation-enrollment"
}

import {
  to = authentik_stage_invitation.invitation_verification
  id = "4ce14eed-6603-4ed6-a374-3ebdd3631f07"
}

import {
  to = authentik_stage_prompt_field.enrollment_username
  id = "d2f65755-4f7e-41f6-a0f1-f3e36ccef4cd"
}

import {
  to = authentik_stage_prompt_field.enrollment_displayname
  id = "b745f39b-0a5a-4266-979c-f19101bd976a"
}

import {
  to = authentik_stage_prompt_field.enrollment_student_id
  id = "2b961a6e-7fbc-4e8e-a0ee-a756549075fb"
}

import {
  to = authentik_stage_prompt.enrollment_user_profile
  id = "d21dab4d-f96a-457a-9a6b-4a2aca986e0f"
}

import {
  to = authentik_stage_prompt.enrollment_user_password
  id = "cb2cc4af-62af-4713-9587-eed53521deff"
}

import {
  to = authentik_stage_user_write.enrollment_user_write
  id = "016b4b63-ef81-447a-b6d8-bf4db1b64f45"
}

import {
  to = authentik_stage_user_login.enrollment_user_login
  id = "2e59c97d-2191-4c34-8bd8-157a2ac9b63a"
}

import {
  to = authentik_flow_stage_binding.enrollment_invitation_bind
  id = "7fca6424-5e84-4c43-ba13-a549680e1c66"
}

import {
  to = authentik_flow_stage_binding.enrollment_user_profile_bind
  id = "4bfc3cda-d8a3-40cc-b1e9-5b5b78365a54"
}

import {
  to = authentik_flow_stage_binding.enrollment_user_password_bind
  id = "8ed690f7-9f27-42d2-9a1f-55a1f6deb1da"
}

import {
  to = authentik_flow_stage_binding.enrollment_user_write_bind
  id = "8561df34-c4b7-4bc4-bc13-88d8eae3b03b"
}

import {
  to = authentik_flow_stage_binding.enrollment_user_login_bind
  id = "b17183c8-cfe1-4265-8f51-38931066af3b"
}
