#!/usr/bin/env bash
# .github/scripts/kube-cert-validate.sh (CSR 検証) を、openssl で作った CSR で検証するユニットテスト。
#
# 使い方:
#   ./scripts/test-kube-cert-validate.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALIDATE="${SCRIPT_DIR}/.github/scripts/kube-cert-validate.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

PASS=0
FAIL=0

# 利用者の openssl.cnf に依存しないよう最小の設定を明示する
cat >"${WORK}/min.cnf" <<'CNF'
[req]
distinguished_name = dn
prompt = no
string_mask = utf8only
[dn]
CN = placeholder
CNF

EXPECTED="github:alice:1001"

openssl ecparam -name prime256v1 -genkey -noout -out "${WORK}/ec.pem"
openssl ecparam -name secp224r1 -genkey -noout -out "${WORK}/ec224.pem"
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "${WORK}/rsa2048.pem" 2>/dev/null
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:1024 -out "${WORK}/rsa1024.pem" 2>/dev/null
openssl genpkey -algorithm ED25519 -out "${WORK}/ed.pem"

mkcsr() { # mkcsr <key> <subj> [extra req args...]  -> stdout
  local key="$1" subj="$2"; shift 2
  openssl req -new -config "${WORK}/min.cnf" -key "${key}" -subj "${subj}" "$@" 2>/dev/null
}

# 拒否の理由が意図どおりであることも確かめる (別の理由で拒否されて通るのを防ぐ)
assert_rc() { # assert_rc <desc> <expected-rc> <input-file> [expected-user] [reason-substring]
  local desc="$1" want="$2" in="$3" user="${4-${EXPECTED}}" reason="${5:-}" out rc
  out="$("${VALIDATE}" "${user}" <"${in}" 2>&1)"; rc=$?
  if [[ "${rc}" -eq "${want}" && ( "${want}" -eq 0 || ( "$(wc -l <<<"${out}")" -eq 1 && "${out}" == *"${reason}"* ) ) ]]; then
    echo "  ✅ ${desc}${out:+ -> ${out}}"
    PASS=$((PASS + 1))
  else
    echo "  ❌ ${desc} (expected rc=${want}, actual rc=${rc}: ${out})"
    FAIL=$((FAIL + 1))
  fi
}

put() { mkcsr "$2" "$3" "${@:4}" >"${WORK}/$1.pem"; }

echo "=== 受理 ==="
put ok-ec "${WORK}/ec.pem" "/CN=${EXPECTED}"
assert_rc "正常 (EC P-256)" 0 "${WORK}/ok-ec.pem"
put ok-rsa "${WORK}/rsa2048.pem" "/CN=${EXPECTED}"
assert_rc "正常 (RSA 2048)" 0 "${WORK}/ok-rsa.pem"
assert_rc "正常 (CRLF 改行)" 0 <(sed 's/$/\r/' "${WORK}/ok-ec.pem")

echo ""
echo "=== subject ==="
put other-cn "${WORK}/ec.pem" "/CN=github:bob:1002"
assert_rc "他人の CN" 1 "${WORK}/other-cn.pem" "${EXPECTED}" "CN が"
put other-id "${WORK}/ec.pem" "/CN=github:alice:9999"
assert_rc "数値 ID が違う CN" 1 "${WORK}/other-id.pem" "${EXPECTED}" "CN が"
put sa-cn "${WORK}/ec.pem" "/CN=system:serviceaccount:argocd:argocd-server"
assert_rc "ServiceAccount 名の CN" 1 "${WORK}/sa-cn.pem" "${EXPECTED}" "CN が"
put with-org "${WORK}/ec.pem" "/O=system:masters/CN=${EXPECTED}"
assert_rc "organization 付き" 1 "${WORK}/with-org.pem" "${EXPECTED}" "subject"
put with-ou "${WORK}/ec.pem" "/OU=x/CN=${EXPECTED}"
assert_rc "OU 付き" 1 "${WORK}/with-ou.pem" "${EXPECTED}" "subject"
put two-cn "${WORK}/ec.pem" "/CN=${EXPECTED}/CN=${EXPECTED}"
assert_rc "CN が 2 つ" 1 "${WORK}/two-cn.pem" "${EXPECTED}" "subject"
put multi-rdn "${WORK}/ec.pem" "/CN=${EXPECTED}+O=system:masters"
assert_rc "CN と O の複数値 RDN" 1 "${WORK}/multi-rdn.pem" "${EXPECTED}" "subject"
put no-cn "${WORK}/ec.pem" "/O=x"
assert_rc "CN なし (O のみ)" 1 "${WORK}/no-cn.pem" "${EXPECTED}" "subject"
put empty-subj "${WORK}/ec.pem" "/"
assert_rc "subject が空" 1 "${WORK}/empty-subj.pem" "${EXPECTED}" "subject"
put prefix-cn "${WORK}/ec.pem" "/CN=${EXPECTED}x"
assert_rc "期待名の前方一致" 1 "${WORK}/prefix-cn.pem" "${EXPECTED}" "CN が"

echo ""
echo "=== 属性・拡張 ==="
put san "${WORK}/ec.pem" "/CN=${EXPECTED}" -addext "subjectAltName=DNS:example.com"
assert_rc "SAN 付き" 1 "${WORK}/san.pem" "${EXPECTED}" "属性"
put ku "${WORK}/ec.pem" "/CN=${EXPECTED}" -addext "keyUsage=digitalSignature"
assert_rc "keyUsage 付き" 1 "${WORK}/ku.pem" "${EXPECTED}" "属性"
put bc "${WORK}/ec.pem" "/CN=${EXPECTED}" -addext "basicConstraints=CA:TRUE"
assert_rc "basicConstraints 付き" 1 "${WORK}/bc.pem" "${EXPECTED}" "属性"
cat >"${WORK}/pw.cnf" <<'CNF'
[req]
distinguished_name = dn
attributes = attrs
prompt = no
string_mask = utf8only
[dn]
CN = github:alice:1001
[attrs]
challengePassword = secret
CNF
openssl req -new -config "${WORK}/pw.cnf" -key "${WORK}/ec.pem" >"${WORK}/pw.pem" 2>/dev/null
assert_rc "challengePassword 属性付き" 1 "${WORK}/pw.pem" "${EXPECTED}" "属性"

echo ""
echo "=== 鍵 ==="
put weak-rsa "${WORK}/rsa1024.pem" "/CN=${EXPECTED}"
assert_rc "RSA 1024 (弱い鍵)" 1 "${WORK}/weak-rsa.pem" "${EXPECTED}" "RSA 鍵"
put weak-ec "${WORK}/ec224.pem" "/CN=${EXPECTED}"
assert_rc "EC P-224 (弱い鍵)" 1 "${WORK}/weak-ec.pem" "${EXPECTED}" "EC 鍵"
put weak-ed "${WORK}/ed.pem" "/CN=${EXPECTED}"
assert_rc "Ed25519 (許可外の種類)" 1 "${WORK}/weak-ed.pem" "${EXPECTED}" "鍵の種類"

echo ""
echo "=== 入力形式 ==="
echo "not a pem" >"${WORK}/garbage.pem"
assert_rc "不正な PEM" 1 "${WORK}/garbage.pem" "${EXPECTED}" "PEM が"
: >"${WORK}/empty.pem"
assert_rc "空入力" 1 "${WORK}/empty.pem" "${EXPECTED}" "PEM が"
cat "${WORK}/ok-ec.pem" "${WORK}/ok-ec.pem" >"${WORK}/multi.pem"
assert_rc "PEM が複数" 1 "${WORK}/multi.pem" "${EXPECTED}" "PEM が"
cat "${WORK}/ok-ec.pem" "${WORK}/other-cn.pem" >"${WORK}/multi2.pem"
assert_rc "正常 CSR の後ろに他人の CSR" 1 "${WORK}/multi2.pem" "${EXPECTED}" "PEM が"
{ echo "junk"; cat "${WORK}/ok-ec.pem"; } >"${WORK}/lead.pem"
assert_rc "PEM の前に余計な行" 1 "${WORK}/lead.pem" "${EXPECTED}" "PEM が"
openssl req -new -config "${WORK}/min.cnf" -key "${WORK}/ec.pem" -subj "/CN=${EXPECTED}" -x509 -days 1 >"${WORK}/cert.pem" 2>/dev/null
assert_rc "証明書 (CSR ではない PEM)" 1 "${WORK}/cert.pem" "${EXPECTED}" "PEM が"
# 署名値の 1 バイトを書き換えて自己署名を壊す
python3 - "${WORK}/ok-ec.pem" "${WORK}/tamper.pem" <<'PY'
import base64, re, sys
src = open(sys.argv[1]).read()
der = bytearray(base64.b64decode("".join(l for l in src.splitlines() if not l.startswith("-----"))))
der[-3] ^= 0xFF
b64 = base64.b64encode(bytes(der)).decode()
body = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
open(sys.argv[2], "w").write(f"-----BEGIN CERTIFICATE REQUEST-----\n{body}\n-----END CERTIFICATE REQUEST-----\n")
PY
assert_rc "自己署名が壊れている" 1 "${WORK}/tamper.pem" "${EXPECTED}" "自己署名"
assert_rc "期待ユーザー名が空" 1 "${WORK}/ok-ec.pem" "" "未指定"

echo ""
echo "${PASS} passed, ${FAIL} failed"
[[ "${FAIL}" -eq 0 ]]
