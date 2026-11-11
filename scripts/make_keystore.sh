#!/usr/bin/env bash
set -euo pipefail
# 部署者自己的签名证书：务必长期保管，丢了无法覆盖升级
KS="${1:-aichat.keystore}"
keytool -genkeypair -v -keystore "$KS" -alias aichat -keyalg RSA -keysize 2048 -validity 10000 \
  -storepass "${KS_PASS:-aichat123}" -keypass "${KS_PASS:-aichat123}" \
  -dname "CN=AI Chat Gateway, OU=Self Hosted, O=Community, L=NA, ST=NA, C=NA"
echo "已生成 $KS；构建时用 -storepass $KS_PASS 覆盖"
