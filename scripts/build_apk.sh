#!/usr/bin/env bash
# 构建自带默认服务器地址的 APK。
# 用法：DEFAULT_SERVER=http://1.2.3.4:8099 bash scripts/build_apk.sh
# 不传 DEFAULT_SERVER 则生成“首次启动要求填写服务器地址”的通用包。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/android"
OUT="${OUT:-$ROOT/build}"
SDK="${ANDROID_SDK:-$HOME/android-sdk}"
BT="$SDK/build-tools/34.0.0"
JAR="$SDK/platforms/android-34/android.jar"
SRV="${DEFAULT_SERVER:-}"
SRV="${SRV%/}"
mkdir -p "$OUT"
WORK="$OUT/apk-src"
rm -rf "$WORK"; cp -r "$SRC" "$WORK"
mkdir -p "$WORK/assets"
cp "$ROOT/server/static/app.html" "$WORK/assets/app.html"
[ -d "$ROOT/android/assets/builtin" ] && mkdir -p "$WORK/assets/builtin" && cp "$ROOT/android/assets/builtin/"* "$WORK/assets/builtin/"
AV=$(grep -o "APP_VER *= *'[^']*'" "$WORK/assets/app.html" | head -1 | grep -o "[0-9][0-9.]*" || true)
FV=$(cat "$ROOT/APP_VERSION" 2>/dev/null || echo "")
if [ -n "$FV" ] && [ "$AV" != "$FV" ]; then
  echo "警告：页面 APP_VER=$AV 与 APP_VERSION=$FV 不一致，客户端会显示旧版本号"
fi
python3 - "$WORK" "$SRV" <<'PY'
import pathlib, sys
work, srv = sys.argv[1], sys.argv[2]
p = pathlib.Path(work)/"res/values/strings.xml"
p.write_text(p.read_text().replace("__QUNQI_SERVER__", srv))
p = pathlib.Path(work)/"assets/app.html"
p.write_text(p.read_text().replace("__QUNQI_SERVER__", srv))
PY
mkdir -p "$WORK/classes" "$WORK/dex" "$WORK/gen"
aapt2 compile --dir "$WORK/res" -o "$WORK/res.zip"
aapt2 link -o "$WORK/base.apk" -I "$JAR" --manifest "$WORK/AndroidManifest.xml" \
  -R "$WORK/res.zip" -A "$WORK/assets" --min-sdk-version 21 --target-sdk-version 34 --auto-add-overlay \
  --java "$WORK/gen"
javac -source 8 -target 8 -encoding UTF-8 -classpath "$JAR" -d "$WORK/classes" $(find "$WORK/src" "$WORK/gen" -name "*.java")
d8 --min-api 21 --lib "$JAR" --output "$WORK/dex" $(find "$WORK/classes" -name "*.class") >/dev/null
cp "$WORK/base.apk" "$WORK/unsigned.apk"
( cd "$WORK" && zip -j unsigned.apk dex/classes.dex >/dev/null )
zipalign -f 4 "$WORK/unsigned.apk" "$WORK/aligned.apk"
KS="${KEYSTORE:-$ROOT/android/build/aichat.keystore}"
apksigner sign --ks "$KS" --ks-pass "pass:${KS_PASS:-aichat123}" --key-pass "pass:${KS_PASS:-aichat123}" \
  --out "$OUT/Qunqi-${VERSION:-dev}.apk" "$WORK/aligned.apk"
echo "OK: $OUT/Qunqi-${VERSION:-dev}.apk  (default_server='$SRV')"
