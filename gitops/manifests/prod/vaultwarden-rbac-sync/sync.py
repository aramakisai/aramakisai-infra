#!/usr/bin/env python3
"""Vaultwarden RBAC Sync エンジン。

Authentikグループメンバーシップを正本として、Vaultwarden Organization/Collectionの
ユーザー単位権限を自動同期する。実行モードは --mode=cron (定期実行) / --mode=serve
(Trigger Receiver常駐) の2種類。
"""
import argparse
import hmac
import json
import logging
import os
import socket
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import quote as url_quote
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOGGER_NAME = "vaultwarden-rbac-sync"

VALID_PERMISSIONS = ("can_view", "can_view_except_passwords", "can_edit", "can_manage")

REQUIRED_MAPPING_FIELDS = ("authentik_group", "organization", "collection_id", "permission")

# Lease名・namespaceはハードコードで固定し、設定ミスを避ける (design.md SyncLockManager Validation)。
K8S_NAMESPACE = "prod"

DEFAULT_MAPPING_PATH = "/etc/vaultwarden-rbac-sync/mapping.json"


class MappingConfigError(Exception):
    """mapping.json の構文・スキーマが不正な場合に発生する (Requirement 4.3)。"""


@dataclass(frozen=True)
class MappingEntry:
    """Vaultwarden Collection は collection_id (UUID) で直接指定する。

    Collection名はOrganization鍵でクライアント側暗号化されたCipherStringとしてのみ
    APIから取得できるため、人間可読名での照合は原理的に成立しない
    (research.md「Vaultwarden Collection名はOrg鍵でクライアント暗号化される」、実機検証済み)。
    collection_label はレビュー用の任意コメントであり、照合には使わない。
    """

    authentik_group: str
    organization: str
    collection_id: str
    permission: str
    collection_label: str | None = None


def load_mapping(raw_json: str) -> list[MappingEntry]:
    """mapping.json の内容を検証しつつ読み込む (Requirement 4.1, 4.2, 4.3, 4.4)。

    1つの authentik_group が複数エントリに現れるケース (4.4) を正しく扱うため、
    グループ名でのグルーピングはせず、エントリのリストをそのまま全件返す。
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise MappingConfigError(f"mapping.json の構文が不正です: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("mappings"), list):
        raise MappingConfigError("mapping.json は {'mappings': [...]} 形式である必要があります")

    entries = []
    for index, raw_entry in enumerate(data["mappings"]):
        if not isinstance(raw_entry, dict):
            raise MappingConfigError(f"mappings[{index}] はオブジェクトである必要があります")

        missing = [field for field in REQUIRED_MAPPING_FIELDS if field not in raw_entry]
        if missing:
            raise MappingConfigError(f"mappings[{index}] に必須フィールドがありません: {missing}")

        permission = raw_entry["permission"]
        if permission not in VALID_PERMISSIONS:
            raise MappingConfigError(
                f"mappings[{index}] の permission が不正です: {permission!r} "
                f"(許可値: {VALID_PERMISSIONS})"
            )

        entries.append(
            MappingEntry(
                authentik_group=raw_entry["authentik_group"],
                organization=raw_entry["organization"],
                collection_id=raw_entry["collection_id"],
                permission=permission,
                collection_label=raw_entry.get("collection_label"),
            )
        )
    return entries


class AuthentikApiError(Exception):
    """Authentik API呼び出しが認証エラー・タイムアウトで失敗した場合に発生する (Requirement 1.3)。

    呼び出し元 (SyncOrchestrator) に伝播し、同期処理全体を中断させる。
    """


@dataclass(frozen=True)
class GroupMembersResult:
    group_name: str
    member_emails: list[str]
    error: str | None


class AuthentikGroupClient:
    """Authentikグループメンバーシップ取得クライアント (Requirement 1.1-1.4)。

    専用APIトークン (PRESENCE_AUTHENTIK_API_TOKEN パターン踏襲) でBearer認証する。
    """

    def __init__(self, base_url: str, api_token: str, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._timeout = timeout

    def get_group_members(self, group_name: str) -> GroupMembersResult:
        """グループ名からメンバーのメールアドレス一覧を取得する (Requirement 1.1)。

        グループが存在しない場合は例外を投げず GroupMembersResult.error に記録する (1.4)。
        認証エラー・タイムアウトは AuthentikApiError として呼び出し元に伝播させる (1.3)。
        """
        url = (
            f"{self._base_url}/api/v3/core/groups/"
            f"?name={url_quote(group_name, safe='')}&include_users=true"
        )
        request = Request(url, headers={"Authorization": f"Bearer {self._api_token}"})
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read())
        except HTTPError as exc:
            raise AuthentikApiError(
                f"Authentik APIエラー (group={group_name}): HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError) as exc:
            raise AuthentikApiError(
                f"Authentik API呼び出しに失敗しました (group={group_name}): {exc}"
            ) from exc

        results = payload.get("results", [])
        if not results:
            log_event("authentik_group_not_found", group=group_name)
            return GroupMembersResult(
                group_name, [], f"グループ '{group_name}' がAuthentik上に存在しません"
            )

        member_emails = [
            user["email"]
            for user in (results[0].get("users_obj") or [])
            if user.get("email")
        ]
        return GroupMembersResult(group_name, member_emails, None)


class VaultwardenApiError(Exception):
    """Vaultwarden API呼び出しが失敗した場合の基底例外。"""


class VaultwardenAuthError(VaultwardenApiError):
    """アクセストークン取得に失敗した場合に発生する (Requirement 2.3)。

    呼び出し元 (SyncOrchestrator) に伝播し、同期処理全体を中断させる。
    """


class VaultwardenOrgClient:
    """Vaultwardenサービスアカウント認証・Organization/Collection操作クライアント (Requirement 2, 3, 6, 7, 8)。

    専用サービスアカウントのUser Personal API Key (client_id=user.<uuid>) で
    client_credentials grantのアクセストークンを取得する。
    """

    def __init__(self, base_url: str, client_id: str, client_secret: str, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._access_token: str | None = None

    def authenticate(self) -> str:
        """Personal API Key (client_credentials grant, scope=api) でアクセストークンを取得する (2.1, 2.2)。

        認証失敗時 (HTTPエラー・タイムアウト・access_token欠落) はVaultwardenAuthErrorを
        投げ、同期処理全体を中断させる (2.3)。
        """
        body = urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": "api",
            }
        ).encode("utf-8")
        request = Request(
            f"{self._base_url}/identity/connect/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read())
        except HTTPError as exc:
            raise VaultwardenAuthError(f"Vaultwarden認証に失敗しました: HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise VaultwardenAuthError(f"Vaultwarden認証呼び出しに失敗しました: {exc}") from exc

        access_token = payload.get("access_token")
        if not access_token:
            raise VaultwardenAuthError("Vaultwarden認証レスポンスにaccess_tokenが含まれていません")

        self._access_token = access_token
        return access_token

    def _request_json(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self._access_token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self._base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raise VaultwardenApiError(
                f"Vaultwarden APIエラー ({method} {path}): HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError) as exc:
            raise VaultwardenApiError(
                f"Vaultwarden API呼び出しに失敗しました ({method} {path}): {exc}"
            ) from exc
        return json.loads(raw) if raw else {}

    def list_organizations(self) -> list["Organization"]:
        """Vaultwarden上の全Organization一覧を取得する (Requirement 3.1)。"""
        payload = self._request_json("GET", "/api/organizations")
        return [Organization(org["id"], org["name"]) for org in payload.get("data", [])]

    def get_org_state(self, org_id: str) -> "OrgState":
        """対象Organizationのメンバー一覧・Collection一覧を取得する (Requirement 3.1, 3.2)。"""
        users_payload = self._request_json("GET", f"/api/organizations/{org_id}/users")
        collections_payload = self._request_json("GET", f"/api/organizations/{org_id}/collections")

        members = [
            Member(
                member_id=user["id"],
                email=user["email"],
                member_type=user["type"],
                status=user["status"],
                collections=[
                    CollectionGrant(c["id"], c["readOnly"], c["hidePasswords"], c["manage"])
                    for c in (user.get("collections") or [])
                ],
            )
            for user in users_payload.get("data", [])
        ]
        collections = [
            Collection(c["id"], c.get("name", "")) for c in collections_payload.get("data", [])
        ]
        return OrgState(members=members, collections=collections)

    def resolve_mapping_collection(self, org_state: "OrgState", collection_id: str) -> str | None:
        """mapping.jsonのcollection_idが対象Organizationに実在するか検証する (Requirement 3.3)。

        Collection名は暗号化CipherStringのためname照合はできない (research.md参照)。
        存在する場合はcollection_idをそのまま返し、存在しない場合はNoneを返す
        (呼び出し元で当該マッピングのみエラー記録し処理を継続する)。
        """
        valid_ids = {c.collection_id for c in org_state.collections}
        return collection_id if collection_id in valid_ids else None

    def invite_member(
        self, org_id: str, email: str, member_type: int, collections: list["CollectionGrant"]
    ) -> None:
        """ユーザーをOrganizationへ招待する (Requirement 6.1, 6.2)。

        招待と同時にCollection権限を指定できる (Confirm完了まで有効化されない、design.md参照)。
        招待失敗時はVaultwardenApiErrorを投げ、呼び出し元 (SyncOrchestrator) が当該ユーザーのみ
        エラー記録し他のユーザーの処理を継続する (6.3)。
        """
        body = {
            "emails": [email],
            "type": member_type,
            "collections": [_collection_grant_to_payload(c) for c in collections],
            "groups": [],
        }
        self._request_json("POST", f"/api/organizations/{org_id}/users/invite", body=body)

    def put_member_collections(
        self, org_id: str, member_id: str, member_type: int, collections: list["CollectionGrant"]
    ) -> None:
        """対象メンバーのCollection権限配列をフルリプレースで更新する (Requirement 7.1, 7.2)。

        未Confirmメンバーにも送信してよい (Confirm完了の瞬間に自動的に有効化される、design.md参照)。
        typeには対象メンバーの現在のtypeをそのまま再送する (Requirement 7.3)。サービスアカウントは
        Admin止まりのため、type変更を伴うPUTは権限昇格ガードにより403になる。
        """
        body = {
            "type": member_type,
            "collections": [_collection_grant_to_payload(c) for c in collections],
            "groups": [],
        }
        self._request_json("PUT", f"/api/organizations/{org_id}/users/{member_id}", body=body)


@dataclass(frozen=True)
class Organization:
    org_id: str
    name: str


@dataclass(frozen=True)
class CollectionGrant:
    collection_id: str
    read_only: bool
    hide_passwords: bool
    manage: bool


@dataclass(frozen=True)
class Collection:
    # name はOrganization鍵でクライアント暗号化されたCipherStringのまま保持する。照合・表示には使わない。
    collection_id: str
    name: str


@dataclass(frozen=True)
class Member:
    member_id: str
    email: str
    member_type: int
    status: int
    collections: list[CollectionGrant]


@dataclass(frozen=True)
class OrgState:
    members: list[Member]
    collections: list[Collection]


# 実機検証済み (research.md「Confirm前のPUTの実際の挙動」、Bitwarden公開APIのMembershipStatus enumと一致):
# Invited=0, Accepted=1, Confirmed=2, Revoked=-1。Confirmed以外は全てConfirm待ち通知の対象とする (6.5)。
VAULTWARDEN_STATUS_CONFIRMED = 2

PERMISSION_TO_COLLECTION_FLAGS = {
    "can_view": (True, False, False),
    "can_view_except_passwords": (True, True, False),
    "can_edit": (False, False, False),
    "can_manage": (False, False, True),
}


def permission_to_collection_grant(collection_id: str, permission: str) -> CollectionGrant:
    """mapping.jsonの権限レベル(4種別)をVaultwardenのCollectionData形式に変換する。

    対応表 (research.md Data Contracts参照):
    can_view -> readOnly/hidePasswords/manage = True/False/False
    can_view_except_passwords -> True/True/False
    can_edit -> False/False/False
    can_manage -> False/False/True
    """
    read_only, hide_passwords, manage = PERMISSION_TO_COLLECTION_FLAGS[permission]
    return CollectionGrant(collection_id, read_only, hide_passwords, manage)


def merge_collection_grants(
    current_grants: list[CollectionGrant], desired_grants: list[CollectionGrant]
) -> list[CollectionGrant]:
    """現状のCollection権限とマッピングに基づく変更をマージする (Requirement 7.1, 7.2)。

    PUTはフルリプレースAPIのため、マッピング対象外のCollection権限は現状値のまま保持し、
    マッピング対象のCollectionのみ上書き・新規追加する (意図しない権限欠落を防ぐ、research.md参照)。
    """
    merged = {grant.collection_id: grant for grant in current_grants}
    for grant in desired_grants:
        merged[grant.collection_id] = grant
    return list(merged.values())


def remove_collection_grants(
    current_grants: list[CollectionGrant], collection_ids_to_remove: set[str]
) -> list[CollectionGrant]:
    """グループ脱退に対応するCollection権限のみを除去する (Requirement 8.1, 8.2, 8.3)。

    指定外のCollection権限は変更しない。Organizationからの除名は行わない
    (呼び出し元はput_member_collectionsで結果を送信するのみで、メンバー自体は削除しない)。
    """
    return [grant for grant in current_grants if grant.collection_id not in collection_ids_to_remove]


def filter_confirm_pending(members: list[Member]) -> list[Member]:
    """招待済みだが未Confirmのメンバーを抽出する (Requirement 6.5)。

    Discord通知 (Confirm待ち一覧) の判定にのみ使う。PUT送信のスキップ判定には使わない
    (未ConfirmメンバーへのPUTはスキップ不要、design.md参照)。
    """
    return [member for member in members if member.status != VAULTWARDEN_STATUS_CONFIRMED]


@dataclass(frozen=True)
class InvitePlan:
    email: str
    org_id: str
    collections: list[CollectionGrant]


@dataclass(frozen=True)
class ConfirmPendingPlan:
    email: str
    org_id: str


@dataclass(frozen=True)
class UpdatePlan:
    member_id: str
    org_id: str
    member_type: int
    collections: list[CollectionGrant]


@dataclass(frozen=True)
class RemovalPlan:
    member_id: str
    org_id: str
    member_type: int
    collection_ids: set[str]


@dataclass(frozen=True)
class SyncPlan:
    invites: list[InvitePlan]
    confirm_pending: list[ConfirmPendingPlan]
    collection_updates: list[UpdatePlan]
    collection_removals: list[RemovalPlan]
    unchanged_count: int


class PermissionDiffEngine:
    """AuthentikグループメンバーシップとVaultwarden現状を比較し、
    招待・更新・削除対象を算出する (Requirement 5.1, 5.2, 5.3, 8.1, 8.2, 8.3)。
    """

    @staticmethod
    def compute_diff(
        mappings: list[MappingEntry],
        group_members: dict[str, list[str]],
        org_states: dict[str, OrgState],
        org_ids: dict[str, str],
    ) -> SyncPlan:
        invites: list[InvitePlan] = []
        confirm_pending: list[ConfirmPendingPlan] = []
        collection_updates: list[UpdatePlan] = []
        collection_removals: list[RemovalPlan] = []
        unchanged_count = 0

        # Build desired state per (email_lower, org_name) -> set of (collection_id, permission)
        desired: dict[tuple[str, str], dict[str, str]] = {}
        for entry in mappings:
            members = group_members.get(entry.authentik_group, [])
            for email in members:
                key = (email.lower(), entry.organization)
                if key not in desired:
                    desired[key] = {}
                desired[key][entry.collection_id] = entry.permission

        # Track which members were processed
        processed_members: set[tuple[str, str]] = set()

        for org_name, org_state in org_states.items():
            org_id = org_ids.get(org_name)
            if not org_id:
                continue

            valid_collection_ids = {c.collection_id for c in org_state.collections}

            for member in org_state.members:
                member_key = (member.email.lower(), org_name)
                processed_members.add(member_key)

                member_desired = desired.get(member_key, {})

                # Check confirm pending
                if member.status != VAULTWARDEN_STATUS_CONFIRMED:
                    confirm_pending.append(ConfirmPendingPlan(email=member.email, org_id=org_id))

                # Calculate desired collections for this member
                desired_grants: list[CollectionGrant] = []
                for cid, perm in member_desired.items():
                    if cid in valid_collection_ids:
                        desired_grants.append(permission_to_collection_grant(cid, perm))

                current_grants = member.collections

                # Determine if update needed
                current_map = {g.collection_id: g for g in current_grants}
                desired_map = {g.collection_id: g for g in desired_grants}

                if current_map == desired_map:
                    if member_desired or not current_grants:
                        unchanged_count += 1
                    else:
                        # Member has collections but no mapping desires any — removal case
                        pass
                else:
                    if desired_grants:
                        collection_updates.append(
                            UpdatePlan(
                                member_id=member.member_id,
                                org_id=org_id,
                                member_type=member.member_type,
                                collections=desired_grants,
                            )
                        )
                    else:
                        # All collections should be removed
                        if current_grants:
                            collection_removals.append(
                                RemovalPlan(
                                    member_id=member.member_id,
                                    org_id=org_id,
                                    member_type=member.member_type,
                                    collection_ids={g.collection_id for g in current_grants},
                                )
                            )

            # Find members to invite (in desired but not in org)
            for (email_lower, org_name_key), member_desired in desired.items():
                if org_name_key != org_name:
                    continue
                if (email_lower, org_name) in processed_members:
                    continue
                org_id = org_ids.get(org_name)
                if not org_id:
                    continue
                desired_grants = [
                    permission_to_collection_grant(cid, perm)
                    for cid, perm in member_desired.items()
                    if cid in valid_collection_ids
                ]
                invites.append(InvitePlan(email=email_lower, org_id=org_id, collections=desired_grants))

        return SyncPlan(
            invites=invites,
            confirm_pending=confirm_pending,
            collection_updates=collection_updates,
            collection_removals=collection_removals,
            unchanged_count=unchanged_count,
        )


def _collection_grant_to_payload(grant: CollectionGrant) -> dict:
    return {
        "id": grant.collection_id,
        "readOnly": grant.read_only,
        "hidePasswords": grant.hide_passwords,
        "manage": grant.manage,
    }


class SyncOrchestrator:
    """同期処理全体のオーケストレーション・dry-run制御 (Requirement 5.3, 6.1, 6.3, 6.4, 6.5, 7.1, 8.1, 9.1, 9.2)。"""

    def __init__(
        self,
        mappings: list[MappingEntry],
        authentik_client: AuthentikGroupClient,
        vaultwarden_client: VaultwardenOrgClient,
        discord_notifier: "DiscordNotifier",
    ):
        self._mappings = mappings
        self._authentik = authentik_client
        self._vaultwarden = vaultwarden_client
        self._discord = discord_notifier

    def run(self, dry_run: bool = False) -> "SyncPlan":
        """同期フローを実行する。

        実行順序:
        1. Vaultwarden認証
        2. Authentikグループメンバー取得
        3. Vaultwarden現状取得
        4. 差分計算
        5. (dry_run=False) 適用
        6. Discord通知
        """
        self._vaultwarden.authenticate()

        # 2. Authentikグループメンバー取得
        group_members: dict[str, list[str]] = {}
        for entry in self._mappings:
            if entry.authentik_group in group_members:
                continue
            result = self._authentik.get_group_members(entry.authentik_group)
            if result.error:
                log_event("group_error", group=entry.authentik_group, error=result.error)
                group_members[entry.authentik_group] = []
            else:
                group_members[entry.authentik_group] = result.member_emails

        # 3. Vaultwarden現状取得
        orgs = self._vaultwarden.list_organizations()
        org_ids = {org.name: org.org_id for org in orgs}
        org_states: dict[str, OrgState] = {}
        for entry in self._mappings:
            org_id = org_ids.get(entry.organization)
            if not org_id:
                log_event("org_not_found", organization=entry.organization)
                continue
            if entry.organization not in org_states:
                org_states[entry.organization] = self._vaultwarden.get_org_state(org_id)

        # 4. 差分計算
        plan = PermissionDiffEngine.compute_diff(
            self._mappings, group_members, org_states, org_ids
        )

        errors: list[str] = []

        # 5. 適用 (dry_run でなければ)
        if not dry_run:
            for invite in plan.invites:
                try:
                    self._vaultwarden.invite_member(
                        invite.org_id,
                        invite.email,
                        2,  # type=User
                        invite.collections,
                    )
                    log_event("invite_sent", email=invite.email, org_id=invite.org_id)
                except VaultwardenApiError as exc:
                    log_event("invite_failed", email=invite.email, error=str(exc))
                    errors.append(f"invite {invite.email}: {exc}")

            for update in plan.collection_updates:
                try:
                    # Merge desired with existing non-target collections via full replace
                    org_state = None
                    for oname, state in org_states.items():
                        if org_ids.get(oname) == update.org_id:
                            org_state = state
                            break
                    member = None
                    if org_state:
                        for m in org_state.members:
                            if m.member_id == update.member_id:
                                member = m
                                break
                    if member:
                        merged = merge_collection_grants(member.collections, update.collections)
                    else:
                        merged = update.collections
                    self._vaultwarden.put_member_collections(
                        update.org_id, update.member_id, update.member_type, merged
                    )
                    log_event("collection_updated", member_id=update.member_id, org_id=update.org_id)
                except VaultwardenApiError as exc:
                    log_event("update_failed", member_id=update.member_id, error=str(exc))
                    errors.append(f"update {update.member_id}: {exc}")

            for removal in plan.collection_removals:
                try:
                    org_state = None
                    for oname, state in org_states.items():
                        if org_ids.get(oname) == removal.org_id:
                            org_state = state
                            break
                    member = None
                    if org_state:
                        for m in org_state.members:
                            if m.member_id == removal.member_id:
                                member = m
                                break
                    if member:
                        new_grants = remove_collection_grants(member.collections, removal.collection_ids)
                        self._vaultwarden.put_member_collections(
                            removal.org_id, removal.member_id, removal.member_type, new_grants
                        )
                        log_event("collection_removed", member_id=removal.member_id, org_id=removal.org_id)
                except VaultwardenApiError as exc:
                    log_event("removal_failed", member_id=removal.member_id, error=str(exc))
                    errors.append(f"removal {removal.member_id}: {exc}")

        # 6. Discord通知
        summary = self._build_summary(plan, dry_run, errors)
        try:
            self._discord.notify(summary)
        except Exception as exc:
            log_event("discord_notify_failed", error=str(exc))

        # Attach errors to plan for test assertions
        object.__setattr__(plan, "errors", errors)
        return plan

    def _build_summary(self, plan: SyncPlan, dry_run: bool, errors: list[str]) -> str:
        mode_label = "[dry-run] " if dry_run else ""
        lines = [
            f"{mode_label}Vaultwarden RBAC Sync 完了",
            f"- 招待: {len(plan.invites)}件",
            f"- 権限更新: {len(plan.collection_updates)}件",
            f"- 権限削除: {len(plan.collection_removals)}件",
            f"- 変更なし: {plan.unchanged_count}件",
            f"- Confirm待ち: {len(plan.confirm_pending)}件",
        ]
        if plan.confirm_pending:
            emails = [p.email for p in plan.confirm_pending]
            lines.append(f"  Confirm待ちユーザー: {', '.join(emails)}")
            lines.append("  Vaultwarden Web UIでConfirm操作を行ってください")
        if errors:
            lines.append("エラー:")
            for err in errors:
                lines.append(f"  - {err}")
        return "\n".join(lines)


def _utc_now_rfc3339() -> str:
    # Kubernetes MicroTime requires microseconds (%f); seconds-only format causes BadRequest.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_rfc3339(value: str) -> datetime:
    """k8sのRFC3339タイムスタンプを解析する (秒以下の桁数がGo由来で揺れるため切り詰める)。"""
    value = value.rstrip("Z")
    if "." in value:
        date_part, frac = value.split(".", 1)
        value = f"{date_part}.{(frac + '000000')[:6]}"
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f")
    else:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    return dt.replace(tzinfo=timezone.utc)


def _is_lease_stale(lease: dict, default_duration: int) -> bool:
    spec = lease.get("spec", {})
    renew_time = spec.get("renewTime") or spec.get("acquireTime")
    if not renew_time:
        return True
    duration = spec.get("leaseDurationSeconds", default_duration)
    age = (datetime.now(timezone.utc) - _parse_rfc3339(renew_time)).total_seconds()
    return age > duration


class SyncLockManager:
    """Kubernetes Lease による排他制御 (Requirement 10.2, 13.3, 13.4)。

    `kubectl create`（dry-runなし、実APIへのPOST）を一次取得手段とする。既に存在する場合のみ
    `kubectl get`でstaleness（leaseDurationSeconds超過）を判定し、stale時のみ
    resourceVersionを伴う`kubectl replace`（楽観的並行性制御、design.md Concurrency strategy）で
    奪取する。新鮮なLeaseが存在する場合は取得失敗として扱う (実際に片方のみ成功する)。
    """

    LEASE_DURATION_SECONDS = 600

    def __init__(self, namespace: str, lease_name: str = "vaultwarden-rbac-sync-lock"):
        self.namespace = namespace
        self.lease_name = lease_name
        self._holder_identity: str | None = None

    def _build_manifest(self, holder: str) -> dict:
        now = _utc_now_rfc3339()
        return {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": {"name": self.lease_name, "namespace": self.namespace},
            "spec": {
                "holderIdentity": holder,
                "leaseDurationSeconds": self.LEASE_DURATION_SECONDS,
                "acquireTime": now,
                "renewTime": now,
            },
        }

    def acquire(self) -> bool:
        """Leaseを取得する。新鮮な (stale でない) Leaseが既に存在する場合はFalseを返す。"""
        import subprocess

        holder = f"{socket.gethostname()}-{os.getpid()}"
        manifest = self._build_manifest(holder)

        create_result = subprocess.run(
            ["kubectl", "create", "-f", "-"],
            input=json.dumps(manifest), capture_output=True, text=True,
        )
        if create_result.returncode == 0:
            self._holder_identity = holder
            log_event("lease_acquired", lease=self.lease_name, namespace=self.namespace, holder=holder)
            return True

        if "AlreadyExists" not in create_result.stderr:
            log_event("lease_acquire_error", lease=self.lease_name, error=create_result.stderr.strip())
            return False

        return self._try_steal_stale_lease(manifest, holder)

    def _try_steal_stale_lease(self, manifest: dict, holder: str) -> bool:
        import subprocess

        get_result = subprocess.run(
            ["kubectl", "get", "lease", self.lease_name, "-n", self.namespace, "-o", "json"],
            capture_output=True, text=True,
        )
        if get_result.returncode != 0:
            log_event("lease_busy", lease=self.lease_name, namespace=self.namespace)
            return False

        current = json.loads(get_result.stdout)
        if not _is_lease_stale(current, self.LEASE_DURATION_SECONDS):
            log_event("lease_busy", lease=self.lease_name, namespace=self.namespace)
            return False

        # 楽観的並行性制御: 取得済みresourceVersionが一致する場合のみreplaceが成功する。
        manifest["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
        replace_result = subprocess.run(
            ["kubectl", "replace", "-f", "-"],
            input=json.dumps(manifest), capture_output=True, text=True,
        )
        if replace_result.returncode == 0:
            self._holder_identity = holder
            log_event("lease_stolen_stale", lease=self.lease_name, namespace=self.namespace, holder=holder)
            return True

        log_event("lease_busy", lease=self.lease_name, namespace=self.namespace)
        return False

    def release(self) -> None:
        """Leaseを解放する。自分が取得した時点のholderIdentityのまま現存する場合のみ削除する

        (staleとして他プロセスに奪取済みのLeaseを誤って削除しないため)。
        """
        import subprocess

        if not self._holder_identity:
            return

        get_result = subprocess.run(
            ["kubectl", "get", "lease", self.lease_name, "-n", self.namespace, "-o", "json"],
            capture_output=True, text=True,
        )
        if get_result.returncode == 0:
            current = json.loads(get_result.stdout)
            current_holder = current.get("spec", {}).get("holderIdentity")
            if current_holder and current_holder != self._holder_identity:
                log_event("lease_release_skipped_not_holder", lease=self.lease_name)
                self._holder_identity = None
                return

        cmd = ["kubectl", "delete", "lease", self.lease_name, "-n", self.namespace, "--ignore-not-found"]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            log_event("lease_released", lease=self.lease_name, namespace=self.namespace)
        except subprocess.CalledProcessError as exc:
            log_event("lease_release_failed", lease=self.lease_name, error=str(exc))
        self._holder_identity = None


class DiscordNotifier:
    """Discord Ops Webhook への通知クライアント (Requirement 11.1, 11.2, 11.3, 11.4)。"""

    def __init__(self, webhook_url: str, timeout: float = 10.0):
        self._webhook_url = webhook_url
        self._timeout = timeout

    def notify(self, message: str) -> None:
        """通知を送信する。失敗しても例外を伝播させない (同期処理の成否に影響しない)。"""
        if not self._webhook_url:
            return
        body = json.dumps({"content": message}, ensure_ascii=False).encode("utf-8")
        request = Request(
            self._webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                pass
        except Exception:
            # 通知失敗は無視 (Requirement 11.4)
            pass


def build_clients_from_env() -> tuple[
    list[MappingEntry], AuthentikGroupClient, VaultwardenOrgClient, DiscordNotifier
]:
    """環境変数とマウント済みmapping.jsonから各クライアントを構築する (Requirement 12.1)。

    Authentik/Vaultwardenのbase URLはクラスター内サービスDNS固定値として
    マニフェストに直接記述される (ldap-outpost等の既存パターン踏襲、シークレットではない)。
    """
    mapping_path = os.environ.get("MAPPING_CONFIG_PATH", DEFAULT_MAPPING_PATH)
    with open(mapping_path, encoding="utf-8") as f:
        mappings = load_mapping(f.read())

    authentik_client = AuthentikGroupClient(
        base_url=os.environ["AUTHENTIK_BASE_URL"],
        api_token=os.environ["AUTHENTIK_API_TOKEN"],
    )
    vaultwarden_client = VaultwardenOrgClient(
        base_url=os.environ["VAULTWARDEN_BASE_URL"],
        client_id=os.environ["VAULTWARDEN_SA_CLIENT_ID"],
        client_secret=os.environ["VAULTWARDEN_SA_CLIENT_SECRET"],
    )
    discord_notifier = DiscordNotifier(os.environ.get("DISCORD_WEBHOOK_URL", ""))
    return mappings, authentik_client, vaultwarden_client, discord_notifier


def run_cron_mode(
    lock_manager: SyncLockManager | None = None,
    client_factory=build_clients_from_env,
) -> int:
    """--mode=cron 起動時のエントリポイント (Requirement 10.1, 10.2, 10.3)。

    Lease取得 → SyncOrchestrator実行 (dry_run=False固定、design.md「本番適用モード固定」) →
    Lease解放の順に実行する。Lease取得に失敗した場合は実行せず exit 0 で正常終了する
    (concurrencyPolicy: Forbid と合わせた二重の安全策)。
    """
    lock_manager = lock_manager or SyncLockManager(namespace=K8S_NAMESPACE)

    if not lock_manager.acquire():
        log_event("cron_skipped_lease_busy")
        return 0

    try:
        mappings, authentik_client, vaultwarden_client, discord_notifier = client_factory()
        orchestrator = SyncOrchestrator(mappings, authentik_client, vaultwarden_client, discord_notifier)
        orchestrator.run(dry_run=False)
    finally:
        lock_manager.release()

    return 0


class TriggerReceiver:
    """POST /trigger の認証・Lease取得・バックグラウンド同期起動を担う (Requirement 13.1-13.4)。

    実ソケット処理 (TriggerHTTPServer) から認証ヘッダのみを受け取りステータスコードを返す、
    http.serverに依存しない単体テスト可能な層。
    """

    def __init__(
        self,
        trigger_token: str,
        lock_manager: SyncLockManager,
        run_sync,
        thread_factory=threading.Thread,
    ):
        self._trigger_token = trigger_token
        self._lock_manager = lock_manager
        self._run_sync = run_sync
        self._thread_factory = thread_factory

    def handle_trigger(self, authorization_header: str | None) -> int:
        """Bearerトークンを検証し、成功時はLease取得後に非同期で同期処理を起動する (13.1, 13.2)。

        Lease取得に失敗した場合も202を返し、次回の定期実行での補完をログに記録するのみとする (13.4)。
        """
        expected = f"Bearer {self._trigger_token}"
        if not authorization_header or not hmac.compare_digest(authorization_header, expected):
            log_event("trigger_unauthorized")
            return 401

        if self._lock_manager.acquire():
            thread = self._thread_factory(target=self._run_locked, daemon=True)
            thread.start()
        else:
            log_event("trigger_lease_busy")

        return 202

    def _run_locked(self) -> None:
        try:
            self._run_sync()
        finally:
            self._lock_manager.release()


def _build_trigger_handler(receiver: TriggerReceiver):
    class TriggerHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/trigger":
                self.send_response(404)
                self.end_headers()
                return
            status = receiver.handle_trigger(self.headers.get("Authorization"))
            self.send_response(status)
            self.end_headers()

        def do_GET(self):
            if self.path == "/healthz":
                self.send_response(200)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):
            log_event("http_access", message=format % args)

    return TriggerHandler


class TriggerHTTPServer:
    """Trigger Receiver用 http.server 常駐サーバー (Requirement 13.1, 13.2)。

    POST /trigger をTriggerReceiverへ委譲し、GET /healthzでliveness probeに応答する。
    """

    def __init__(self, receiver: TriggerReceiver, host: str = "0.0.0.0", port: int = 8080):
        handler_cls = _build_trigger_handler(receiver)
        self._httpd = ThreadingHTTPServer((host, port), handler_cls)

    @property
    def server_port(self) -> int:
        return self._httpd.server_port

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        self._httpd.shutdown()


def run_serve_mode() -> int:
    """--mode=serve 起動時のエントリポイント (Requirement 13.1, 13.2)。

    Trigger Receiverとして常駐し、Authentikからのイベント (ログイン・グループ変更) を
    POST /trigger で受け付け、SyncLockManagerでの排他制御を介して即時同期を起動する。
    """
    lock_manager = SyncLockManager(namespace=K8S_NAMESPACE)
    trigger_token = os.environ["TRIGGER_TOKEN"]
    port = int(os.environ.get("PORT", "8080"))

    def run_sync() -> None:
        mappings, authentik_client, vaultwarden_client, discord_notifier = build_clients_from_env()
        orchestrator = SyncOrchestrator(mappings, authentik_client, vaultwarden_client, discord_notifier)
        orchestrator.run(dry_run=False)

    receiver = TriggerReceiver(trigger_token, lock_manager, run_sync)
    server = TriggerHTTPServer(receiver, port=port)
    log_event("serve_started", port=port)
    server.serve_forever()
    return 0


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def log_event(event: str, **fields) -> None:
    """招待/更新/削除件数等を後続タスクで埋め込める構造化ログ出力 (Requirement 11.1)。"""
    logging.getLogger(LOGGER_NAME).info(json.dumps({"event": event, **fields}, ensure_ascii=False))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Vaultwarden RBAC Sync エンジン")
    parser.add_argument(
        "--mode",
        choices=("cron", "serve"),
        required=True,
        help="cron: CronJobからの定期実行 / serve: Trigger Receiverとして常駐",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_arg_parser().parse_args(argv)

    log_event("startup", mode=args.mode)

    if args.mode == "cron":
        return run_cron_mode()
    return run_serve_mode()


if __name__ == "__main__":
    sys.exit(main())
