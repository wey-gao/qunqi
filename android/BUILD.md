# 手工构建 APK（无需 Gradle）

依赖：JDK 17、Android SDK（platforms/android-34、build-tools/34.0.0）。

```bash
export JAVA_HOME=~/jdk17 PATH=~/jdk17/bin:~/android-sdk/build-tools/34.0.0:$PATH
cp ../server/static/app.html assets/app.html   # 内置页面与线上同步
aapt2 compile --dir res -o build/res.zip
aapt2 link -o build/base.apk -I $ANDROID_JAR --manifest AndroidManifest.xml \
  -R build/res.zip -A assets --min-sdk-version 21 --target-sdk-version 34 --auto-add-overlay
javac -source 8 -target 8 -encoding UTF-8 -classpath $ANDROID_JAR -d build/classes $(find src -name '*.java')
d8 --min-api 21 --lib $ANDROID_JAR --output build/dex $(find build/classes -name '*.class')
cp build/base.apk build/unsigned.apk && cd build && zip -j unsigned.apk dex/classes.dex
zipalign -f 4 unsigned.apk aligned.apk
KS_PASS=你的密钥库口令 apksigner sign --ks aichat.keystore --ks-pass "pass:$KS_PASS" --key-pass "pass:$KS_PASS" --out ../AI-Chat.apk aligned.apk
```

注意：`AndroidManifest.xml` 必须显式写 `android:versionCode`，否则部分 ROM 会判定安装包损坏；每版同时更新页面里的 `APP_VER`。
