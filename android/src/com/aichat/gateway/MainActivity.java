package com.aichat.gateway;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.text.InputType;
import android.view.KeyEvent;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.PermissionRequest;
import android.webkit.JavascriptInterface;
import android.media.MediaRecorder;
import android.util.Base64;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.net.Uri;
import android.content.Intent;
import android.os.Build;
import android.Manifest;
import android.content.pm.PackageManager;
import java.util.ArrayList;

public class MainActivity extends Activity {
    /* 界面内置在 APK 里，启动不再等网络；接口仍连服务器 */
    private String LOCAL_URL = "";
    private String DEFAULT_URL = "";
    private String FALLBACK_URL = "";
    private WebView web;
    private SharedPreferences sp;
    private android.webkit.ValueCallback<Uri[]> fileCb;
    private MediaRecorder rec;
    private String recPath;
    private long recStart;
    private String lastB64 = "";
    private static final int REQ_FILE = 1001;
    private static final int REQ_CAMERA = 1002;
    private static final int REQ_CAM_PERM = 1005;
    private Uri cameraOut;
    static boolean visible = false;
    static boolean isVisible() { return visible; }

    @Override
    protected void onResume() { super.onResume(); visible = true; }
    @Override
    protected void onPause() { visible = false; super.onPause(); }

    private void initServerUrls() {
        String srv = getString(R.string.default_server);
        if (srv == null || srv.contains("__QUNQI")) srv = "";
        srv = srv.trim();
        if (srv.endsWith("/")) srv = srv.substring(0, srv.length() - 1);
        LOCAL_URL = srv.isEmpty() ? "" : srv + "/";
        DEFAULT_URL = srv;
        FALLBACK_URL = srv;
    }

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        initServerUrls();
        try { init(); } catch (Throwable e) { showError(e); }
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission("android.permission.POST_NOTIFICATIONS") != PackageManager.PERMISSION_GRANTED) {
            try { requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 1010); } catch (Throwable ignored) {}
        }
    }

    private void showError(Throwable e) {
        String msg = "启动失败：" + e.getClass().getSimpleName() + "\n" + e.getMessage();
        android.widget.TextView tv = new android.widget.TextView(this);
        tv.setText(msg + "\n\n点此重试");
        tv.setPadding(40, 120, 40, 40);
        tv.setTextColor(Color.parseColor("#1b2026"));
        tv.setBackgroundColor(Color.WHITE);
        tv.setOnClickListener(v -> { try { init(); } catch (Throwable e2) { showError(e2); } });
        setContentView(tv);
    }

    private void init() {
        sp = getSharedPreferences("app", MODE_PRIVATE);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.WHITE);
        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setAllowFileAccess(true);
        s.setAllowFileAccessFromFileURLs(true);
        s.setAllowUniversalAccessFromFileURLs(true);
        web.setBackgroundColor(Color.WHITE);
        web.setDownloadListener((url, ua, cd, mime, len) -> {
            try { startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url))); } catch (Exception ignored) {}
        });
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String url) { return false; }
            @Override
            public void onPageFinished(WebView v, String u) {
                super.onPageFinished(v, u);
                v.evaluateJavascript("(document.getElementById('hName')?1:0)", value -> {
                    if (value != null && value.contains("0")) {
                        // 本地页没渲染出来，兜底走远程，避免黑屏
                        v.postDelayed(() -> {
                            if (v.getContentHeight() <= 0) v.loadUrl(FALLBACK_URL);
                        }, 1200);
                    }
                });
            }
            @Override
            public void onReceivedError(WebView v, int code, String desc, String failingUrl) {
                // 主站打不开时先退到直连 IP；直连也失败就弹窗让用户改地址
                if (failingUrl != null && failingUrl.startsWith("file:")) {
                    // 本地资源异常时才退回远程页面
                    if (url() == null || url().startsWith("file:")) v.loadUrl(DEFAULT_URL);
                    else v.loadUrl(url());
                } else if (failingUrl != null && failingUrl.startsWith(DEFAULT_URL)) {
                    v.loadUrl(FALLBACK_URL);
                }
            }
        });
        web.addJavascriptInterface(new RecBridge(), "NativeRec");
        web.addJavascriptInterface(new PushBridge(), "NativePush");
        web.addJavascriptInterface(new PermBridge(), "NativePerm");
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView v, android.webkit.ValueCallback<Uri[]> cb,
                                             FileChooserParams params) {
                if (fileCb != null) { fileCb.onReceiveValue(null); }
                fileCb = cb;
                try {
                    if (params.isCaptureEnabled()) {
                        // 带 capture 的图片选择：直接唤起系统相机，拍到后回传 Uri
                        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                            requestPermissions(new String[]{Manifest.permission.CAMERA}, REQ_CAM_PERM);
                            return true;
                        }
                        startCamera();
                        return true;
                    }
                    Intent i = params.createIntent();
                    i.addCategory(Intent.CATEGORY_OPENABLE);
                    startActivityForResult(i, REQ_FILE);
                    return true;
                } catch (Exception e) {
                    fileCb = null;
                    return false;
                }
            }
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(() -> request.grant(request.getResources()));
            }
        });
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);
        askPermissions();
        // 始终用固定 base 加载内置页：换过服务器地址也不再改变 localStorage 归属，
        // 否则同一台手机会被当成“新用户”，表现为关掉 App 就掉登录
        loadLocalPage();
    }

    /* 原生录音：WebView 在 http 源下拿不到 getUserMedia，改由原生录 m4a 再回传 base64 */
    class PermBridge {
        @JavascriptInterface
        public String status() {
            if (!bridgeAllowed()) return "{}";
            try {
                boolean n = Build.VERSION.SDK_INT < 33 || checkSelfPermission("android.permission.POST_NOTIFICATIONS") == PackageManager.PERMISSION_GRANTED;
                android.os.PowerManager pm = (android.os.PowerManager) getSystemService(POWER_SERVICE);
                boolean b = pm != null && pm.isIgnoringBatteryOptimizations(getPackageName());
                return "{\"notif\":" + n + ",\"battery\":" + b + "}";
            } catch (Throwable e) { return "{}"; }
        }

        @JavascriptInterface
        public void request() {
            if (!bridgeAllowed()) return;
            runOnUiThread(() -> {
                try {
                    if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission("android.permission.POST_NOTIFICATIONS") != PackageManager.PERMISSION_GRANTED) {
                        requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 1010);
                    }
                    android.os.PowerManager pm = (android.os.PowerManager) getSystemService(POWER_SERVICE);
                    if (pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName())) {
                        Intent i = new Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
                        i.setData(android.net.Uri.parse("package:" + getPackageName()));
                        startActivity(i);
                    }
                } catch (Throwable ignored) {}
            });
        }

        @JavascriptInterface
        public void openAutostart() {
            runOnUiThread(() -> {
                String brand = Build.BRAND == null ? "" : Build.BRAND.toLowerCase();
                String[][] pages = {
                        {"xiaomi", "com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity"},
                        {"redmi", "com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity"},
                        {"huawei", "com.huawei.systemmanager", "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"},
                        {"honor", "com.hihonor.systemmanager", "com.hihonor.systemmanager.startupmgr.ui.StartupNormalAppListActivity"},
                        {"oppo", "com.coloros.safecenter", "com.coloros.safecenter.permission.startup.StartupAppListActivity"},
                        {"vivo", "com.vivo.permissionmanager", "com.vivo.permissionmanager.activity.BgStartUpManagerActivity"},
                        {"samsung", "com.samsung.android.lool", "com.samsung.android.sm.ui.battery.BatteryActivity"}
                };
                for (String[] pg : pages) {
                    if (brand.contains(pg[0])) {
                        try {
                            Intent i = new Intent();
                            i.setClassName(pg[1], pg[2]);
                            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                            startActivity(i);
                            return;
                        } catch (Throwable ignored) {}
                    }
                }
                try {
                    Intent i = new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
                    i.setData(android.net.Uri.parse("package:" + getPackageName()));
                    startActivity(i);
                } catch (Throwable ignored) {}
            });
        }
    }

    /** 只有内置本地页面或注入的服务器页面能用原生桥，防止用户手填的任意站点调用录音/轮询 */
    private boolean bridgeAllowed() {
        try {
            String u = web == null ? null : web.getUrl();
            if (u == null) return false;
            if (u.startsWith("file:")) return true;
            String host = android.net.Uri.parse(u).getHost();
            if (host == null || DEFAULT_URL.isEmpty()) return false;
            String allow = android.net.Uri.parse(DEFAULT_URL).getHost();
            return allow != null && host.equalsIgnoreCase(allow);
        } catch (Throwable t) {
            return false;
        }
    }

    class PushBridge {
        @JavascriptInterface
        public void start(String device, String secret, String base) {
            if (!bridgeAllowed()) return;
            try {
                SharedPreferences p = PushService.prefs(MainActivity.this);
                p.edit().putString("device", device == null ? "" : device)
                        .putString("secret", secret == null ? "" : secret)
                        .putString("base", base == null ? "" : base)
                        .apply();
                if (p.getLong("since", 0) <= 0) p.edit().putLong("since", System.currentTimeMillis()).apply();
                Intent i = new Intent(MainActivity.this, PushService.class);
                if (Build.VERSION.SDK_INT >= 26) startForegroundService(i); else startService(i);
            } catch (Throwable ignored) {}
        }

        @JavascriptInterface
        public void setDnd(boolean on) {
            if (!bridgeAllowed()) return;
            try {
                PushService.prefs(MainActivity.this).edit().putBoolean("dnd", on).apply();
            } catch (Throwable ignored) {}
        }
    }

    class RecBridge {
        @JavascriptInterface
        public boolean startRec() {
            if (!bridgeAllowed()) return false;
            try {
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1003);
                    return false;
                }
                if (rec != null) { try { rec.release(); } catch (Exception ignored) {} rec = null; }
                recPath = new java.io.File(getCacheDir(), "rec.m4a").getAbsolutePath();
                if (Build.VERSION.SDK_INT >= 31) rec = new MediaRecorder(MainActivity.this);
                else rec = new MediaRecorder();
                rec.setAudioSource(MediaRecorder.AudioSource.MIC);
                rec.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4);
                rec.setAudioEncoder(MediaRecorder.AudioEncoder.AAC);
                rec.setAudioSamplingRate(44100);
                rec.setAudioEncodingBitRate(64000);
                rec.setOutputFile(recPath);
                rec.prepare();
                rec.start();
                recStart = System.currentTimeMillis();
                lastB64 = "";
                return true;
            } catch (Throwable e) {
                try { if (rec != null) rec.release(); } catch (Exception ignored) {}
                rec = null;
                return false;
            }
        }

        @JavascriptInterface
        public String stopRec() {
            if (!bridgeAllowed()) return "";
            if (rec == null) return lastB64;
            int dur = (int) ((System.currentTimeMillis() - recStart) / 1000);
            try { rec.stop(); } catch (Throwable ignored) {}
            try { rec.release(); } catch (Throwable ignored) {}
            rec = null;
            try {
                java.io.File f = new java.io.File(recPath);
                if (!f.exists()) return "";
                byte[] buf = new byte[(int) f.length()];
                java.io.FileInputStream in = new java.io.FileInputStream(f);
                int read = 0;
                while (read < buf.length) {
                    int n = in.read(buf, read, buf.length - read);
                    if (n <= 0) break;
                    read += n;
                }
                in.close();
                lastB64 = Base64.encodeToString(buf, Base64.NO_WRAP);
                return "{\"dur\":" + dur + ",\"b64\":\"" + lastB64 + "\"}";
            } catch (Throwable e) {
                return "";
            }
        }

        @JavascriptInterface
        public String permState() {
            StringBuilder sb = new StringBuilder("{\"mic\":");
            sb.append(checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED);
            sb.append(",\"camera\":");
            sb.append(checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED);
            sb.append("}");
            return sb.toString();
        }

        @JavascriptInterface
        public void openAppSettings() {
            try {
                Intent i = new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
                i.setData(Uri.parse("package:" + getPackageName()));
                startActivity(i);
            } catch (Throwable ignored) {}
        }

        @JavascriptInterface
        public void vibrate(int ms) {
            try {
                android.os.Vibrator v = (android.os.Vibrator) getSystemService(VIBRATOR_SERVICE);
                if (v != null) v.vibrate(ms);
            } catch (Throwable ignored) {}
        }

        @JavascriptInterface
        public boolean askMic() {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) return true;
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1003);
            return false;
        }

        @JavascriptInterface
        public boolean askMedia() {
            try {
                if (Build.VERSION.SDK_INT >= 33) {
                    if (checkSelfPermission("android.permission.READ_MEDIA_IMAGES") != PackageManager.PERMISSION_GRANTED) {
                        requestPermissions(new String[]{"android.permission.READ_MEDIA_IMAGES", Manifest.permission.CAMERA}, 1004);
                        return false;
                    }
                } else if (checkSelfPermission(Manifest.permission.READ_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(new String[]{Manifest.permission.READ_EXTERNAL_STORAGE, Manifest.permission.CAMERA}, 1004);
                    return false;
                }
                return true;
            } catch (Throwable e) { return true; }
        }

        @JavascriptInterface
        public boolean hasMic() {
            return checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
        }
    }

    private void askPermissions() {
        try {
            if (Build.VERSION.SDK_INT < 23) return;
            final ArrayList<String> need = new ArrayList<>();
            addPerm(need, Manifest.permission.RECORD_AUDIO);
            addPerm(need, Manifest.permission.CAMERA);
            if (Build.VERSION.SDK_INT >= 33) addPerm(need, "android.permission.READ_MEDIA_IMAGES");
            else addPerm(need, Manifest.permission.READ_EXTERNAL_STORAGE);
            if (Build.VERSION.SDK_INT >= 33) addPerm(need, "android.permission.POST_NOTIFICATIONS");
            if (need.isEmpty()) return;
            new AlertDialog.Builder(this)
                .setTitle("允许以下权限")
                .setMessage("麦克风：发送语音条、语音通话\n相机/相册：发图片、设置表情\n通知：新消息提醒\n\n只用于这些功能，不会上传通讯录。")
                .setPositiveButton("去允许", (d, w) -> requestPermissions(need.toArray(new String[0]), 1002))
                .setNegativeButton("稍后", null)
                .show();
        } catch (Throwable ignored) {}
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] res) {
        super.onRequestPermissionsResult(req, perms, res);
        if (req == REQ_CAM_PERM) {
            boolean ok = res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED;
            if (ok) startCamera();
            else {
                if (fileCb != null) { fileCb.onReceiveValue(null); fileCb = null; }
                android.widget.Toast.makeText(this, "没有相机权限，无法拍照", android.widget.Toast.LENGTH_SHORT).show();
            }
            return;
        }
        if (req != 1002 && req != 1003) return;
        for (int i = 0; i < perms.length; i++) {
            if (res[i] != PackageManager.PERMISSION_GRANTED) {
                new AlertDialog.Builder(this)
                    .setTitle("权限未开启")
                    .setMessage("没有权限时对应功能不可用。可以到“设置 → 权限”再打开系统权限页。")
                    .setPositiveButton("去设置", (d, w) -> {
                        try {
                            Intent it = new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
                            it.setData(Uri.parse("package:" + getPackageName()));
                            startActivity(it);
                        } catch (Throwable ignored) {}
                    })
                    .setNegativeButton("知道了", null)
                    .show();
                break;
            }
        }
    }

    private void addPerm(ArrayList<String> list, String p) {
        if (checkSelfPermission(p) != PackageManager.PERMISSION_GRANTED) list.add(p);
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        if (req == REQ_CAMERA) {
            Uri[] out = null;
            if (res == RESULT_OK) {
                if (cameraOut != null) out = new Uri[]{ cameraOut };
                else if (data != null && data.getData() != null) out = new Uri[]{ data.getData() };
                else if (data != null && data.getExtras() != null && data.getExtras().get("data") instanceof android.graphics.Bitmap) {
                    android.graphics.Bitmap bmp = (android.graphics.Bitmap) data.getExtras().get("data");
                    out = new Uri[]{ saveTempBitmap(bmp) };
                }
            }
            if (out == null && cameraOut != null) {
                try { getContentResolver().delete(cameraOut, null, null); } catch (Exception ignored) {}
            }
            cameraOut = null;
            if (fileCb != null) { fileCb.onReceiveValue(out); fileCb = null; }
            return;
        }
        if (req == REQ_FILE) {
            Uri[] out = null;
            if (res == RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    int n = data.getClipData().getItemCount();
                    out = new Uri[n];
                    for (int i = 0; i < n; i++) out[i] = data.getClipData().getItemAt(i).getUri();
                } else if (data.getData() != null) {
                    out = new Uri[]{ data.getData() };
                }
            }
            if (fileCb != null) { fileCb.onReceiveValue(out); fileCb = null; }
            return;
        }
        super.onActivityResult(req, res, data);
    }

    private void startCamera() {
        try {
            cameraOut = getContentResolver().insert(
                android.provider.MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                new android.content.ContentValues());
            Intent cam = new Intent(android.provider.MediaStore.ACTION_IMAGE_CAPTURE);
            if (cameraOut != null) cam.putExtra(android.provider.MediaStore.EXTRA_OUTPUT, cameraOut);
            startActivityForResult(cam, REQ_CAMERA);
        } catch (Exception e) {
            if (fileCb != null) { fileCb.onReceiveValue(null); fileCb = null; }
            android.widget.Toast.makeText(this, "无法打开相机", android.widget.Toast.LENGTH_SHORT).show();
        }
    }

    private Uri saveTempBitmap(android.graphics.Bitmap bmp) {
        try {
            java.io.File f = new java.io.File(getCacheDir(), "shot-" + System.currentTimeMillis() + ".jpg");
            java.io.FileOutputStream fos = new java.io.FileOutputStream(f);
            bmp.compress(android.graphics.Bitmap.CompressFormat.JPEG, 90, fos);
            fos.close();
            return Uri.fromFile(f);
        } catch (Exception e) { return null; }
    }

    private String url() {
        String u = sp.getString("url", "");
        return (u == null || u.isEmpty()) ? LOCAL_URL : u;
    }

    private void loadLocalPage() {
        try {
            java.io.InputStream in = getAssets().open("app.html");
            java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            in.close();
            String html = bos.toString("UTF-8");
            // 不在安装包里内置任何设备身份，首次启动由页面自己生成
            if (html.length() < 1000) throw new IllegalStateException("asset empty");
            String base = LOCAL_URL;
            if(base == null || base.isEmpty()){
                String saved = sp.getString("url", "");
                base = (saved == null || saved.isEmpty()) ? "https://qunqi.local/" : saved;
            }
            web.loadDataWithBaseURL(base, html, "text/html; charset=utf-8", "utf-8", null);
        } catch (Exception e) {
            web.loadUrl(LOCAL_URL);
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent e) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web.canGoBack()) { web.goBack(); return true; }
        return super.onKeyDown(keyCode, e);
    }

    @SuppressWarnings("unused")
    private void changeServer() {
        EditText et = new EditText(this);
        et.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        et.setText(url());
        new AlertDialog.Builder(this)
            .setTitle("服务器地址")
            .setView(et)
            .setPositiveButton("保存", (d, w) -> {
                sp.edit().putString("url", et.getText().toString().trim()).apply();
                web.loadUrl(url());
            })
            .setNegativeButton("取消", null)
            .show();
    }
}
