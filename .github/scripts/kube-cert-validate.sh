#!/usr/bin/env bash
# 人向けクライアント証明書の CSR 検証。発行ワークフローと scripts/test-kube-cert-validate.sh が共用する。
#
#   kube-cert-validate.sh <期待ユーザー名> < csr.pem
#
# 成功時は 0。失敗時は理由 1 行を stdout に出して 1 で終了する。
# 期待ユーザー名 (github:<login>:<id>) は呼び出し側が github コンテキストから組み立てる。

set -uo pipefail

reject() { echo "CSR を拒否: $*"; exit 1; }

expected="${1:-}"
[[ -n "${expected}" ]] || reject "期待ユーザー名が未指定です"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

head -c 8193 >"${tmp}/in.pem"
(($(stat -c %s "${tmp}/in.pem") <= 8192)) || reject "入力が大きすぎます"

# PEM の外側に余計な行があると、他の CSR や任意データを紛れ込ませられるため 1 ブロックだけを許す
awk '
  /^-----BEGIN (NEW )?CERTIFICATE REQUEST-----\r?$/ { if (state != 0) bad = 1; state = 1; b++; next }
  /^-----END (NEW )?CERTIFICATE REQUEST-----\r?$/   { if (state != 1) bad = 1; state = 2; e++; next }
  state == 1 && /^[A-Za-z0-9+\/=]+\r?$/ { next }
  /^[ \t\r]*$/ { next }
  { bad = 1 }
  END { exit (bad || b != 1 || e != 1 || state != 2) ? 1 : 0 }
' "${tmp}/in.pem" || reject "PEM が CERTIFICATE REQUEST 1 つだけではありません"

openssl req -in "${tmp}/in.pem" -outform DER -out "${tmp}/csr.der" 2>/dev/null || reject "PKCS#10 として読めません"
openssl req -inform DER -in "${tmp}/csr.der" -verify -noout >/dev/null 2>&1 || reject "自己署名の検証に失敗しました"

# req -text は属性・拡張がなくても見出しを出すため、ASN.1 の構造で判定する
openssl asn1parse -inform DER -in "${tmp}/csr.der" >"${tmp}/asn1.txt" 2>/dev/null || reject "ASN.1 として読めません"

# CertificationRequestInfo の直下は version・subject・spki・[0] attributes の 4 要素だけ。
# subject は SET 1 個、その中は AttributeTypeAndValue 1 個 (commonName と文字列)。
structure="$(awk '
  { match($0, /d= *[0-9]+/); d = substr($0, RSTART + 2, RLENGTH - 2) + 0
    line = $0; sub(/^[^:]*:d= *[0-9]+ +hl= *[0-9]+ +l= *[0-9]+ +/, "", line) }
  d == 1 { top++ }
  d == 2 && top == 1 { n2++; kind2[n2] = line; len2[n2] = $0; sub(/.*l= */, "", len2[n2]); sub(/ .*/, "", len2[n2]) }
  d == 3 && top == 1 && n2 == 2 { n3++ }
  d == 4 && top == 1 && n2 == 2 { n4++ }
  d == 5 && top == 1 && n2 == 2 { n5++; kind5[n5] = line }
  END {
    if (n2 != 4) { print "attrs"; exit }
    if (kind2[4] !~ /^cons: cont \[ 0 \]/ || len2[4] != 0) { print "attrs"; exit }
    if (n3 != 1 || n4 != 1 || n5 != 2) { print "subject"; exit }
    if (kind5[1] !~ /commonName$/ || kind5[2] !~ /^prim: UTF8STRING/) { print "subject"; exit }
    print "ok"
  }
' "${tmp}/asn1.txt")"
case "${structure}" in
  ok) ;;
  attrs) reject "属性または拡張要求 (SAN・keyUsage 等) を含んでいます" ;;
  *) reject "subject が CN 1 属性だけではありません" ;;
esac

cn="$(openssl req -inform DER -in "${tmp}/csr.der" -noout -subject -nameopt RFC2253 2>/dev/null)"
[[ "${cn}" == "subject=CN=${expected}" ]] || reject "CN が期待ユーザー名と一致しません"

text="$(openssl req -inform DER -in "${tmp}/csr.der" -noout -text 2>/dev/null)"
algo="$(sed -n 's/^ *Public Key Algorithm: //p' <<<"${text}" | head -1)"
bits="$(sed -n 's/^ *Public-Key: (\([0-9]*\) bit)/\1/p' <<<"${text}" | head -1)"
case "${algo}" in
  rsaEncryption) if ! [[ "${bits}" =~ ^[0-9]+$ ]] || ((bits < 2048)); then reject "RSA 鍵は 2048 bit 以上が必要です"; fi ;;
  id-ecPublicKey)
    curve="$(sed -n 's/^ *ASN1 OID: //p' <<<"${text}" | head -1)"
    [[ "${curve}" =~ ^(prime256v1|secp384r1|secp521r1)$ ]] || reject "EC 鍵は P-256 以上が必要です" ;;
  *) reject "鍵の種類が許可されていません (EC または RSA のみ)" ;;
esac

echo "ok: ${expected}"
