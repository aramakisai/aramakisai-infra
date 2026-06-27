#!/usr/bin/env python3
# /// script
# dependencies = ["cryptography"]
# ///
"""
Vaultwarden org_key 復元スクリプト。

bootstrap 時に生成した org 対称鍵を管理者のマスターパスワードで復元し、
VAULTWARDEN_ORG_KEY として出力する。

使い方 (対話モード):
  uv run scripts/recover-org-key.py

使い方 (env var モード — CI/非対話):
  VW_EMAIL=admin@aramakisai.com \\  # confidential:allow
  VW_CLIENT_ID=user.<uuid> \\
  VW_CLIENT_SECRET=<secret> \\
  VW_MASTER_PASSWORD=<password> \\
  uv run scripts/recover-org-key.py

  または Infisical でSAクレデンシャルを自動注入:
  VW_EMAIL=<sa_email> VW_MASTER_PASSWORD=<sa_password> \\
    infisical run --env=prod -- uv run scripts/recover-org-key.py
  (VAULTWARDEN_SA_CLIENT_ID / VAULTWARDEN_SA_CLIENT_SECRET が自動注入される)

必要な情報:
  - VW_EMAIL        : Vaultwarden アカウントのメールアドレス
  - VW_CLIENT_ID    : Personal API Key client_id (user.<uuid>)
                      未設定時は VAULTWARDEN_SA_CLIENT_ID を使用
  - VW_CLIENT_SECRET: Personal API Key client_secret
                      未設定時は VAULTWARDEN_SA_CLIENT_SECRET を使用
  - VW_MASTER_PASSWORD: マスターパスワード (env var 未設定時は対話入力)
  - 対象 Organization ID: b7a4c50d-ee91-4fe4-b11d-f0b31209abd6
"""
import base64
import getpass
import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.parse import urlencode

try:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
    from cryptography.hazmat.primitives import hashes, hmac as crypto_hmac, serialization
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
except ImportError:
    print("ERROR: cryptography パッケージが必要です。uv run で実行してください。", file=sys.stderr)
    sys.exit(1)

VW_URL = os.environ.get("VW_URL", "https://vault.aramakisai.com")
ORG_ID = os.environ.get("VW_ORG_ID", "b7a4c50d-ee91-4fe4-b11d-f0b31209abd6")


# ---------------------------------------------------------------------------
# Bitwarden CipherString パーサー
# ---------------------------------------------------------------------------

def parse_cs(cs: str):
    """CipherString を (type, iv, ct, mac) に分解する。"""
    type_str, rest = cs.split(".", 1)
    t = int(type_str)
    parts = rest.split("|")
    if t == 0:  # AesCbc256_B64
        return t, base64.b64decode(parts[0]), base64.b64decode(parts[1]), None
    if t == 2:  # AesCbc256_HmacSha256_B64
        return t, base64.b64decode(parts[0]), base64.b64decode(parts[1]), base64.b64decode(parts[2])
    if t == 4:  # Rsa2048_OaepSha1_B64
        return t, None, base64.b64decode(parts[0]), None
    raise ValueError(f"Unknown CipherString type: {t}")


def aes_cbc_decrypt(sym_key: bytes, iv: bytes, ct: bytes) -> bytes:
    """AES-256-CBC 復号 + PKCS7 アンパディング。sym_key は最初の 32 バイトを使う。"""
    cipher = Cipher(algorithms.AES(sym_key[:32]), modes.CBC(iv))
    dec = cipher.decryptor()
    padded = dec.update(ct) + dec.finalize()
    pad_len = padded[-1]
    return padded[:-pad_len]


def verify_and_decrypt(sym_key_64: bytes, cs: str) -> bytes:
    """64バイト対称鍵で CipherString (type 0 or 2) を検証・復号する。"""
    t, iv, ct, mac = parse_cs(cs)
    enc_key = sym_key_64[:32]
    mac_key = sym_key_64[32:]
    if t == 2 and mac is not None:
        h = crypto_hmac.HMAC(mac_key, hashes.SHA256())
        h.update(iv + ct)
        computed = h.finalize()
        if computed != mac:
            raise ValueError("MAC検証失敗: マスターパスワードが正しくないかデータが破損しています")
    return aes_cbc_decrypt(enc_key, iv, ct)


def rsa_oaep_sha1_decrypt(priv_key_der: bytes, ct: bytes) -> bytes:
    """RSA-OAEP-SHA1 で復号する。"""
    private_key = serialization.load_der_private_key(priv_key_der, password=None)
    return private_key.decrypt(
        ct,
        asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA1()),
            algorithm=hashes.SHA1(),
            label=None,
        ),
    )


# ---------------------------------------------------------------------------
# Bitwarden 鍵導出
# ---------------------------------------------------------------------------

def derive_master_key(password: str, email: str, kdf_type: int, iterations: int,
                      memory_mb: int = 64, parallelism: int = 4) -> bytes:
    """マスターパスワードからマスターキー (32 bytes) を導出する。"""
    salt = email.strip().lower().encode("utf-8")
    pw = password.encode("utf-8")
    if kdf_type == 0:  # PBKDF2-SHA256
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
        return kdf.derive(pw)
    if kdf_type == 1:  # Argon2id
        try:
            from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
            kdf = Argon2id(
                salt=salt,
                length=32,
                iterations=iterations,
                lanes=parallelism,
                memory_cost=memory_mb * 1024,
            )
            return kdf.derive(pw)
        except ImportError:
            try:
                from argon2.low_level import hash_secret_raw, Type
                return hash_secret_raw(
                    secret=pw, salt=salt, time_cost=iterations,
                    memory_cost=memory_mb * 1024, parallelism=parallelism,
                    hash_len=32, type=Type.ID,
                )
            except ImportError:
                print("ERROR: Argon2id KDF が必要です。argon2-cffi を追加してください。", file=sys.stderr)
                sys.exit(1)
    raise ValueError(f"Unknown KDF type: {kdf_type}")


def stretch_master_key(master_key: bytes) -> bytes:
    """マスターキー (32 bytes) を 64 バイト対称鍵に拡張する (HKDF-Expand)。"""
    enc = HKDFExpand(algorithm=hashes.SHA256(), length=32, info=b"enc").derive(master_key)
    mac = HKDFExpand(algorithm=hashes.SHA256(), length=32, info=b"mac").derive(master_key)
    return enc + mac


# ---------------------------------------------------------------------------
# Vaultwarden API ヘルパー
# ---------------------------------------------------------------------------

_UA = "BitwardenDesktop/2024.6.0"


def vw_get(path: str, token: str) -> dict:
    req = Request(
        f"{VW_URL}{path}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": _UA},
    )
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def authenticate(client_id: str, client_secret: str) -> str:
    from urllib.error import HTTPError
    body = urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "api",
        "device_type": "21",
        "device_identifier": "recover-org-key",
        "device_name": "recover-org-key",
    }).encode()
    req = Request(
        f"{VW_URL}/identity/connect/token",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "BitwardenDesktop/2024.6.0",
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())["access_token"]
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"ERROR: 認証失敗 HTTP {e.code}", file=sys.stderr)
        print(f"  レスポンスボディ: {err_body}", file=sys.stderr)
        sys.exit(1)


def prelogin(email: str) -> dict:
    body = json.dumps({"email": email}).encode()
    req = Request(
        f"{VW_URL}/api/accounts/prelogin",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": _UA},
    )
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():
    # クレデンシャル取得 (env var 優先、未設定時は対話入力)
    email = os.environ.get("VW_EMAIL", "")
    client_id = os.environ.get("VW_CLIENT_ID") or os.environ.get("VAULTWARDEN_SA_CLIENT_ID", "")
    client_secret = os.environ.get("VW_CLIENT_SECRET") or os.environ.get("VAULTWARDEN_SA_CLIENT_SECRET", "")
    master_password = os.environ.get("VW_MASTER_PASSWORD", "")

    print("Vaultwarden org_key 復元ツール")
    print(f"対象 URL : {VW_URL}")
    print(f"Org ID   : {ORG_ID}")
    print()

    if not email:
        email = input("アカウントのメールアドレス: ").strip()
    else:
        print(f"VW_EMAIL : {email}")

    if not client_id:
        client_id = input("Personal API Key client_id (user.<uuid>): ").strip()
    else:
        print(f"client_id: {client_id[:12]}...")

    if not client_secret:
        client_secret = getpass.getpass("Personal API Key client_secret: ")

    if not master_password:
        master_password = getpass.getpass("マスターパスワード: ")

    print()
    print("[1] Vaultwarden 認証中...")
    token = authenticate(client_id, client_secret)

    print("[2] Prelogin データ取得...")
    pre = prelogin(email)
    kdf_type = pre.get("kdfType", pre.get("Kdf", 0))
    iterations = pre.get("kdfIterations", pre.get("KdfIterations", 600000))
    memory_mb = pre.get("kdfMemory", pre.get("KdfMemory", 64))
    parallelism = pre.get("kdfParallelism", pre.get("KdfParallelism", 4))
    print(f"    KDF type={kdf_type}, iterations={iterations}")

    print("[3] マスターキー導出中...")
    master_key = derive_master_key(
        master_password, email, kdf_type, iterations, memory_mb, parallelism
    )
    stretched = stretch_master_key(master_key)

    print("[4] Sync データ取得...")
    sync = vw_get("/api/sync?excludeDomains=true", token)
    profile = sync.get("Profile") or sync.get("profile")
    if profile is None:
        print(f"ERROR: sync レスポンスに Profile キーがありません。キー一覧: {list(sync.keys())}", file=sys.stderr)
        sys.exit(1)

    print("[5] ユーザー対称鍵を復号中...")
    user_key_cs = profile.get("Key") or profile.get("key")
    user_sym_key = verify_and_decrypt(stretched, user_key_cs)  # 64 bytes

    print("[6] RSA 秘密鍵を復号中...")
    private_key_cs = profile.get("PrivateKey") or profile.get("privateKey")
    private_key_der = verify_and_decrypt(user_sym_key, private_key_cs)

    print("[7] Org 対称鍵を復号中...")
    orgs = profile.get("Organizations") or profile.get("organizations") or []
    org_key_cs = None
    for org in orgs:
        org_uuid = org.get("Id") or org.get("id") or ""
        if org_uuid.lower() == ORG_ID.lower():
            org_key_cs = org.get("Key") or org.get("key")
            break

    if not org_key_cs:
        print(f"ERROR: Org {ORG_ID} が見つかりません", file=sys.stderr)
        print("  利用可能な Org:", [
            (o.get("Id") or o.get("id"), o.get("Name") or o.get("name"))
            for o in orgs
        ], file=sys.stderr)
        sys.exit(1)

    _, _, org_key_ct, _ = parse_cs(org_key_cs)
    org_key_bytes = rsa_oaep_sha1_decrypt(private_key_der, org_key_ct)

    org_key_b64 = base64.b64encode(org_key_bytes).decode()
    print(f"\n✓ 復元成功 ({len(org_key_bytes)} bytes)")
    print()
    print("以下のコマンドで Infisical に登録してください:")
    print()
    print(f'  infisical secrets set --env=prod VAULTWARDEN_ORG_KEY="{org_key_b64}" >/dev/null 2>&1')
    print()
    print(f"VAULTWARDEN_ORG_KEY={org_key_b64}")


if __name__ == "__main__":
    main()
