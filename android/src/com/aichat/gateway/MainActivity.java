package com.aichat.gateway;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
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
import android.webkit.WebResourceResponse;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.net.Uri;
import android.content.Intent;
import android.os.Build;
import android.Manifest;
import android.content.pm.PackageManager;
import java.util.ArrayList;
import android.animation.ValueAnimator;
import android.graphics.drawable.ColorDrawable;
import android.graphics.drawable.GradientDrawable;
import android.view.View;
import android.view.animation.AccelerateDecelerateInterpolator;
import android.view.animation.DecelerateInterpolator;
import android.content.ContentValues;
import android.provider.MediaStore;
import android.media.MediaScannerConnection;
import android.os.Environment;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

public class MainActivity extends Activity {
    /* 界面内置在 APK 里，启动不再等网络；接口仍连服务器 */
    private String LOCAL_URL = "";
    private String DEFAULT_URL = "";
    private String FALLBACK_URL = "";
    private WebView web;
    private String pendingJoin = null;
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
    protected void onResume() {
        super.onResume();
        visible = true;
        try { PushService.ensureChannels(this); PushService.startIfConfigured(this); } catch (Throwable ignored) {}
        startCredSync();
        try { askBatteryWhitelist(); } catch (Throwable ignored) {}
    }
    @Override
    protected void onPause() { visible = false; stopCredSync(); super.onPause(); }

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
        try { PushService.ensureChannels(this); PushService.startIfConfigured(this); } catch (Throwable ignored) {}
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission("android.permission.POST_NOTIFICATIONS") != PackageManager.PERMISSION_GRANTED) {
            try { requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 1010); } catch (Throwable ignored) {}
        }
    }

    /* 原生凭据同步：直接从页面读 DEV/SECRET/API_BASE 写进 push 配置并拉起后台服务。
       不经过 JavascriptInterface，桥被 allowlist 拦下时也能生效。 */
    private android.os.Handler credH;
    private Runnable credR;

    private void startCredSync() {
        if (credH == null) credH = new android.os.Handler(android.os.Looper.getMainLooper());
        if (credR != null) return;
        credR = new Runnable() {
            public void run() {
                pullCreds();
                if (credH != null) credH.postDelayed(this, 8000);
            }
        };
        credH.postDelayed(credR, 1200);
    }

    private void stopCredSync() {
        if (credH != null && credR != null) credH.removeCallbacks(credR);
    }

    private void pullCreds() {
        if (web == null) return;
        try {
            String js = "(function(){try{return encodeURIComponent(JSON.stringify({"
                    + "d:(typeof DEV==='string'?DEV:''),"
                    + "s:(typeof SECRET==='string'?SECRET:''),"
                    + "b:(typeof API_BASE==='string'?API_BASE:'')}))}catch(e){return ''}})()";
            web.evaluateJavascript(js, val -> {
                try {
                    if (val == null) return;
                    String raw = val;
                    if (raw.startsWith("\"") && raw.endsWith("\"")) raw = raw.substring(1, raw.length() - 1);
                    raw = java.net.URLDecoder.decode(raw, "UTF-8");
                    org.json.JSONObject o = new org.json.JSONObject(raw);
                    String d = o.optString("d", "");
                    String sec = o.optString("s", "");
                    String base = o.optString("b", "");
                    if (d.length() == 0 || base.length() == 0) return;
                    if (base.startsWith("file:")) return;
                    SharedPreferences p = PushService.prefs(this);
                    p.edit().putString("device", d)
                            .putString("secret", sec)
                            .putString("base", base).apply();
                    if (p.getLong("since", 0) <= 0) p.edit().putLong("since", System.currentTimeMillis()).apply();
                    if (!PushService.isRunning()) {
                        Intent i2 = new Intent(this, PushService.class);
                        if (Build.VERSION.SDK_INT >= 26) startForegroundService(i2); else startService(i2);
                    }
                } catch (Throwable ignored) {}
            });
        } catch (Throwable ignored) {}
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
        root.setBackgroundColor(Color.parseColor("#0F1218"));
        try { getWindow().setBackgroundDrawable(new ColorDrawable(Color.parseColor("#0F1218"))); } catch (Exception ignored) {}
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
        web.setBackgroundColor(Color.parseColor("#0F1218"));
        web.setDownloadListener((url, ua, cd, mime, len) -> {
            try {
                if (url != null && url.contains("/api/apk")) {
                    downloadAndInstall(url);
                } else {
                    // 图片/文件：走应用内下载，直接落到相册或下载目录，不再跳浏览器
                    String nm = cd != null ? cd : "";
                    int q = nm.indexOf("filename=");
                    if (q >= 0) nm = nm.substring(q + 9).replace("\"", "").trim();
                    if (nm.isEmpty()) nm = fileNameFromUrl(url, mime);
                    downloadToDevice(url, nm, mime);
                }
            } catch (Exception ignored) {}
        });
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String url) {
                // 更新包链接：应用内下载后直接调起安装，不跳浏览器
                try {
                    if (url != null && url.contains("/api/apk")) {
                        downloadAndInstall(url);
                        return true;
                    }
                } catch (Exception ignored) {}
                return false;
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(WebView v, android.webkit.WebResourceRequest req) {
                // 内置动图背景直接从 APK assets 读，秒开且不耗流量
                try {
                    String path = req.getUrl().getPath();
                    if (path != null && path.startsWith("/builtin/")) {
                        String name = path.substring("/builtin/".length());
                        if (name.contains("..") || name.contains("/")) return null;
                        java.io.InputStream is = getAssets().open("builtin/" + name);
                        String mime = name.endsWith(".webp") ? "image/webp" : "application/octet-stream";
                        return new WebResourceResponse(mime, null, is);
                    }
                } catch (Exception ignored) {}
                return null;
            }
            @Override
            public void onPageFinished(WebView v, String u) {
                super.onPageFinished(v, u);
                try { v.evaluateJavascript("window.__QUNQI_NATIVE=1;", null); } catch (Throwable ignored) {}
                v.postDelayed(() -> { try { pullCreds(); } catch (Throwable ignored) {} }, 2500);
                if (pendingJoin != null) {
                    final String code = pendingJoin;
                    pendingJoin = null;
                    // 等页面完成注册/登录（首次启动要建身份）再自动进群
                    v.postDelayed(() -> {
                        String js = "try{tryJoin(" + jsStr(code) + ", true);}catch(e){}";
                        v.evaluateJavascript(js, null);
                    }, 1800);
                }
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
        web.addJavascriptInterface(new ClipBridge(), "NativeClip");
        web.addJavascriptInterface(new SaveBridge(), "NativeSave");
        web.addJavascriptInterface(new NotifyBridge(), "NativeNotify");
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
        playSplash(root);
        pendingJoin = joinCodeFrom(getIntent());
        askPermissions();
        // 始终用固定 base 加载内置页：换过服务器地址也不再改变 localStorage 归属，
        // 否则同一台手机会被当成“新用户”，表现为关掉 App 就掉登录
        loadLocalPage();
    }


    /* 原生启动动画：深色块从中心小圆角淡入并铺满全屏，再淡出进入聊天界面（不再显示图标） */
    private void playSplash(final FrameLayout root) {
        try {
            final int w = getResources().getDisplayMetrics().widthPixels;
            final int h = getResources().getDisplayMetrics().heightPixels;
            final float d = getResources().getDisplayMetrics().density;
            final int start = (int) (96 * d);

            final View plate = new View(this);
            final GradientDrawable gd = new GradientDrawable();
            gd.setColor(Color.parseColor("#0F1218"));
            gd.setCornerRadius(26 * d);
            plate.setBackground(gd);

            final FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(start, start);
            lp.leftMargin = (w - start) / 2;
            lp.topMargin = (h - start) / 2;
            root.addView(plate, lp);
            plate.setAlpha(0f);

            ValueAnimator anim = ValueAnimator.ofFloat(0f, 1f);
            anim.setDuration(980);
            anim.setInterpolator(new AccelerateDecelerateInterpolator());
            anim.addUpdateListener(va -> {
                float f = va.getAnimatedFraction();
                if (f <= 0.24f) {
                    // 阶段一：小圆角块淡入
                    float p = f / 0.24f;
                    plate.setAlpha(p * 0.95f);
                } else if (f <= 0.66f) {
                    // 阶段二：铺满全屏，圆角收到 0
                    float p = (f - 0.24f) / 0.42f;
                    float ease = (float) (1 - Math.pow(1 - p, 2.4));
                    int size = (int) (start + (w - start) * ease);
                    int hei = (int) (start + (h - start) * ease);
                    lp.width = size; lp.height = hei;
                    lp.leftMargin = (w - size) / 2;
                    lp.topMargin = (h - hei) / 2;
                    plate.setLayoutParams(lp);
                    gd.setCornerRadius(Math.max(0f, (26 * d) * (1 - ease)));
                    plate.setAlpha(1f);
                } else if (f <= 0.78f) {
                    // 阶段三：全屏停留
                    lp.width = w; lp.height = h; lp.leftMargin = 0; lp.topMargin = 0;
                    plate.setLayoutParams(lp);
                    gd.setCornerRadius(0);
                    plate.setAlpha(1f);
                } else {
                    // 阶段四：淡出
                    float p = (f - 0.78f) / 0.22f;
                    lp.width = w; lp.height = h; lp.leftMargin = 0; lp.topMargin = 0;
                    plate.setLayoutParams(lp);
                    gd.setCornerRadius(0);
                    plate.setAlpha(1f - p);
                }
            });
            anim.addListener(new android.animation.AnimatorListenerAdapter() {
                @Override public void onAnimationEnd(android.animation.Animator a) {
                    try { root.removeView(plate); } catch (Exception ignored) {}
                }
            });
            anim.start();
        } catch (Exception ignored) {}
    }

    /** 从 qunqi://join/<code> 里取邀请码 */
    private String joinCodeFrom(Intent it) {
        try {
            if (it == null || it.getData() == null) return null;
            Uri d = it.getData();
            if (!"qunqi".equals(d.getScheme())) return null;
            String code = d.getLastPathSegment();
            if (code == null || code.isEmpty()) code = d.getQueryParameter("code");
            if (code == null) return null;
            code = code.replaceAll("[^A-Za-z0-9_-]", "");
            return code.isEmpty() ? null : code;
        } catch (Exception e) { return null; }
    }

    private static String jsStr(String s) {
        return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'";
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        try {
            String conv = intent.getStringExtra("conv");
            if (conv != null && !conv.isEmpty() && web != null) {
                web.postDelayed(() -> web.evaluateJavascript(
                        "try{window.openConv&&openConv(" + jsStr(conv) + ");}catch(e){}", null), 500);
            }
        } catch (Throwable ignored) {}
        String code = joinCodeFrom(intent);
        if (code != null) {
            pendingJoin = code;
            if (web != null && web.getUrl() != null) {
                web.postDelayed(() -> {
                    String js = "try{tryJoin(" + jsStr(code) + ", true);}catch(e){}";
                    web.evaluateJavascript(js, null);
                }, 600);
                pendingJoin = null;
            }
        }
    }

    /* 省电白名单跳转：部分 ROM 屏蔽 ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS，
       逐级退到“省电策略列表页”再到“应用详情”，保证点了一定有反应 */
    private void openBatteryWhitelist() {
        if (Build.VERSION.SDK_INT < 23) {
            android.widget.Toast.makeText(this, "当前系统版本无需设置省电白名单", android.widget.Toast.LENGTH_SHORT).show();
            return;
        }
        try {
            android.os.PowerManager pm = (android.os.PowerManager) getSystemService(POWER_SERVICE);
            if (pm != null && pm.isIgnoringBatteryOptimizations(getPackageName())) {
                android.widget.Toast.makeText(this, "已经在不受限制名单里，无需再设置", android.widget.Toast.LENGTH_SHORT).show();
                return;
            }
        } catch (Throwable ignored) {}
        Intent[] tries = new Intent[]{
                new Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                        .setData(android.net.Uri.parse("package:" + getPackageName())),
                new Intent(android.provider.Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS),
                new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                        .setData(android.net.Uri.parse("package:" + getPackageName()))
        };
        for (Intent it : tries) {
            try {
                it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(it);
                return;
            } catch (Throwable ignored) {}
        }
        android.widget.Toast.makeText(this, "打不开省电设置，请手动在系统设置 > 应用 > 群栖 里允许后台运行", android.widget.Toast.LENGTH_LONG).show();
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
                    boolean needNotif = false;
                    try {
                        needNotif = Build.VERSION.SDK_INT >= 33
                                && checkSelfPermission("android.permission.POST_NOTIFICATIONS") != PackageManager.PERMISSION_GRANTED;
                    } catch (Throwable ignored) {}
                    if (needNotif) {
                        // 先弹通知授权，再跳省电白名单，避免两个系统界面互相顶掉
                        requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 1010);
                        web.postDelayed(() -> openBatteryWhitelist(), 1200);
                    } else {
                        openBatteryWhitelist();
                    }
                } catch (Throwable t) {
                    android.widget.Toast.makeText(MainActivity.this, "打开系统设置失败，请手动设置", android.widget.Toast.LENGTH_SHORT).show();
                }
            });
        }

        @JavascriptInterface
        public void openQQ(String uin) {
            if (!bridgeAllowed()) return;
            final String u = (uin == null || uin.isEmpty()) ? "2134892918" : uin;
            runOnUiThread(() -> {
                try {
                    Intent i = new Intent(Intent.ACTION_VIEW, android.net.Uri.parse("mqqwpa://im/chat?chat_type=wpa&uin=" + u + "&version=1&src_type=web"));
                    i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(i);
                } catch (Throwable t) {
                    try {
                        Intent i2 = new Intent(Intent.ACTION_VIEW, android.net.Uri.parse("https://qm.qq.com/q/" + u));
                        i2.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                        startActivity(i2);
                    } catch (Throwable t2) {
                        android.widget.Toast.makeText(MainActivity.this, "没找到 QQ，请手动加 " + u, android.widget.Toast.LENGTH_LONG).show();
                    }
                }
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

    /** 应用内下载：图片进相册，其它进“下载/Qunqi”，不再跳浏览器 */
    private void downloadToDevice(final String url, final String rawName, final String mime) {
        if (url == null || !(url.startsWith("http://") || url.startsWith("https://"))) return;
        new Thread(() -> {
            InputStream in = null;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(15000);
                c.setReadTimeout(60000);
                c.setInstanceFollowRedirects(true);
                c.setRequestProperty("User-Agent", "Qunqi/1.0");
                String ctype = c.getContentType();
                String type = (ctype != null && ctype.contains(";")) ? ctype.substring(0, ctype.indexOf(';')) : ctype;
                if (type == null || type.isEmpty()) type = mime;
                boolean image = type != null && type.startsWith("image/");
                String name = safeFileName(rawName, type, url);
                in = c.getInputStream();
                if (Build.VERSION.SDK_INT >= 29) {
                    ContentValues v = new ContentValues();
                    v.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
                    if (type != null) v.put(MediaStore.MediaColumns.MIME_TYPE, type);
                    v.put(MediaStore.MediaColumns.RELATIVE_PATH, image ? "Pictures/Qunqi" : "Download/Qunqi");
                    v.put(MediaStore.MediaColumns.IS_PENDING, 1);
                    Uri coll = image ? MediaStore.Images.Media.EXTERNAL_CONTENT_URI : MediaStore.Downloads.EXTERNAL_CONTENT_URI;
                    Uri item = getContentResolver().insert(coll, v);
                    if (item == null) throw new java.io.IOException("无法创建媒体条目");
                    OutputStream out = getContentResolver().openOutputStream(item);
                    copy(in, out);
                    v.clear(); v.put(MediaStore.MediaColumns.IS_PENDING, 0);
                    getContentResolver().update(item, v, null, null);
                } else {
                    if (checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
                        runOnUiThread(() -> requestPermissions(new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, 1011));
                        toastUi("请允许存储权限后再保存");
                        return;
                    }
                    File dir = new File(Environment.getExternalStoragePublicDirectory(
                            image ? Environment.DIRECTORY_PICTURES : Environment.DIRECTORY_DOWNLOADS), "Qunqi");
                    if (!dir.exists()) dir.mkdirs();
                    File f = new File(dir, name);
                    OutputStream out = new FileOutputStream(f);
                    copy(in, out);
                    MediaScannerConnection.scanFile(this, new String[]{f.getAbsolutePath()}, new String[]{type}, null);
                }
                toastUi(image ? "已保存到相册" : "已保存到下载目录");
            } catch (Throwable e) {
                toastUi("保存失败：" + (e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage()));
            } finally {
                try { if (in != null) in.close(); } catch (Exception ignored) {}
            }
        }).start();
    }

    private void copy(InputStream in, OutputStream out) throws java.io.IOException {
        try {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        } finally {
            try { out.close(); } catch (Exception ignored) {}
        }
    }

    private void toastUi(final String msg) {
        runOnUiThread(() -> {
            try { android.widget.Toast.makeText(MainActivity.this, msg, android.widget.Toast.LENGTH_SHORT).show(); } catch (Throwable ignored) {}
        });
    }

    private String safeFileName(String raw, String type, String url) {
        String name = raw == null ? "" : raw.trim();
        if (name.isEmpty()) name = fileNameFromUrl(url, type);
        StringBuilder sb = new StringBuilder();
        String bad = "/\\:*?\"<>|";
        for (char ch : name.toCharArray()) sb.append(bad.indexOf(ch) >= 0 ? '_' : ch);
        name = sb.toString();
        if (name.length() > 90) {
            int dot = name.lastIndexOf('.');
            String ext = dot > 0 ? name.substring(dot) : "";
            name = name.substring(0, 80) + ext;
        }
        if (name.indexOf('.') < 0) name = name + extFor(type, url);
        return name;
    }

    private String fileNameFromUrl(String url, String type) {
        try {
            String p = Uri.parse(url).getLastPathSegment();
            if (p != null && !p.isEmpty()) return p + extFor(type, url);
        } catch (Throwable ignored) {}
        return "qunqi-" + System.currentTimeMillis() + extFor(type, url);
    }

    private String extFor(String type, String url) {
        if (type != null) {
            if (type.contains("jpeg")) return ".jpg";
            if (type.contains("png")) return ".png";
            if (type.contains("webp")) return ".webp";
            if (type.contains("gif")) return ".gif";
            if (type.contains("mp4")) return ".mp4";
            if (type.contains("pdf")) return ".pdf";
        }
        String u = url == null ? "" : url.toLowerCase();
        int q = u.indexOf('?');
        if (q > 0) u = u.substring(0, q);
        int dot = u.lastIndexOf('.');
        if (dot > 0 && u.length() - dot <= 6) return u.substring(dot);
        return "";
    }

    class SaveBridge {
        /** 页面里点“保存/下载”时调用；返回 true 表示已交给原生处理 */
        @android.webkit.JavascriptInterface
        public boolean save(final String url, final String name) {
            if (!bridgeAllowed()) return false;
            if (url == null || url.trim().isEmpty()) return false;
            try {
                downloadToDevice(url.trim(), name, null);
                return true;
            } catch (Throwable e) { return false; }
        }
    }

    /** 只有内置本地页面或注入的服务器页面能用原生桥，防止用户手填的任意站点调用录音/轮询 */
    private boolean bridgeAllowed() {
        try {
            String u = web == null ? null : web.getUrl();
            if (u == null) return false;
            if (u.startsWith("file:")) return true;
            String host = android.net.Uri.parse(u).getHost();
            if (host == null) return false;
            if (host.equalsIgnoreCase("qunqi.local")) return true;
            // 内置页 base 可能是注入地址，也可能是用户自己填的网关；两者都要放行
            String allow = DEFAULT_URL.isEmpty() ? null : android.net.Uri.parse(DEFAULT_URL).getHost();
            String localHost = LOCAL_URL.isEmpty() ? null : android.net.Uri.parse(LOCAL_URL).getHost();
            if (allow != null && host.equalsIgnoreCase(allow)) return true;
            return localHost != null && host.equalsIgnoreCase(localHost);
        } catch (Throwable t) {
            return false;
        }
    }

    private boolean batAsked = false;
    private void askBatteryWhitelist() {
        if (batAsked || Build.VERSION.SDK_INT < 23) return;
        if ("1".equals(sp.getString("bat_asked", ""))) return;
        try {
            android.os.PowerManager pm = (android.os.PowerManager) getSystemService(POWER_SERVICE);
            if (pm != null && pm.isIgnoringBatteryOptimizations(getPackageName())) return;
        } catch (Throwable ignored) {}
        batAsked = true;
        runOnUiThread(() -> {
            try {
                new AlertDialog.Builder(MainActivity.this)
                        .setTitle("让通知不掉线")
                        .setMessage("系统省电策略可能会杀掉后台轮询。把群栖加入“不限制耗电”名单，后台消息才稳定。")
                        .setPositiveButton("去设置", (d, w) -> {
                            sp.edit().putString("bat_asked", "1").apply();
                            openBatteryWhitelist();
                        })
                        .setNegativeButton("暂不", (d, w) -> sp.edit().putString("bat_asked", "1").apply())
                        .show();
            } catch (Throwable ignored) {}
        });
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
                askBatteryWhitelist();
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

    /* 剪贴板桥：http 源下 navigator.clipboard 不可用，改由原生写系统剪贴板 */
    class ClipBridge {
        @android.webkit.JavascriptInterface
        public boolean copy(String t) {
            try {
                final String txt = t == null ? "" : t;
                Runnable r = () -> {
                    try {
                        android.content.ClipboardManager cm =
                            (android.content.ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
                        cm.setPrimaryClip(android.content.ClipData.newPlainText("qunqi", txt));
                    } catch (Exception ignored) {}
                };
                r.run();
                return true;
            } catch (Exception e) { return false; }
        }
        @android.webkit.JavascriptInterface
        public String paste() {
            try {
                android.content.ClipboardManager cm =
                    (android.content.ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
                if (cm.hasPrimaryClip() && cm.getPrimaryClip() != null
                        && cm.getPrimaryClip().getItemCount() > 0) {
                    CharSequence cs = cm.getPrimaryClip().getItemAt(0).coerceToText(MainActivity.this);
                    return cs == null ? "" : cs.toString();
                }
            } catch (Exception ignored) {}
            return "";
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

    /* ---------- 更新包：应用内下载 + 直接调起系统安装 ---------- */
    private static final String APK_AUTHORITY = "com.aichat.gateway.apk";

    private void downloadAndInstall(final String url) {
        if (url == null) return;
        toastUi("正在下载更新包…");
        new Thread(() -> {
            InputStream in = null;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(15000);
                c.setReadTimeout(120000);
                c.setInstanceFollowRedirects(true);
                c.setRequestProperty("User-Agent", "Qunqi/1.0");
                in = c.getInputStream();
                // 文件名带时间戳，避免上一版包残留被当成新包安装
                File out = new File(getCacheDir(), "update_" + System.currentTimeMillis() + ".apk");
                FileOutputStream fo = new FileOutputStream(out);
                copy(in, fo);
                try { fo.getFD().sync(); } catch (Throwable ignored) {}
                fo.close();
                final long len = out.length();
                runOnUiThread(() -> {
                    if (len < 100 * 1024) { toastUi("更新包下载不完整，请重试"); return; }
                    installApk(out);
                });
            } catch (Throwable e) {
                final String m = (e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage());
                runOnUiThread(() -> toastUi("更新包下载失败：" + m));
            } finally {
                try { if (in != null) in.close(); } catch (Exception ignored) {}
            }
        }).start();
    }

    private void installApk(File apk) {
        try {
            // 先核对包内版本，避免把旧包/残留文件当成新版本装上去
            android.content.pm.PackageInfo pi = getPackageManager()
                    .getPackageArchiveInfo(apk.getAbsolutePath(), 0);
            String pkgName = pi == null ? null : pi.packageName;
            String newVer = pi == null ? "?" : pi.versionName;
            int newCode = pi == null ? -1 : pi.versionCode;
            int curCode = -1;
            String curVer = "?";
            try {
                android.content.pm.PackageInfo cur = getPackageManager()
                        .getPackageInfo(getPackageName(), 0);
                curCode = cur.versionCode;
                curVer = cur.versionName;
            } catch (Throwable ignored) {}
            if (pi == null || !getPackageName().equals(pkgName)) {
                toastUi("下载的文件不是有效的群栖安装包，已取消");
                apk.delete();
                return;
            }
            if (newCode > 0 && curCode > 0 && newCode <= curCode) {
                toastUi("下载到的是旧包 " + newVer + "（当前 " + curVer + "），已取消，请稍后重试");
                apk.delete();
                return;
            }
            toastUi("正在安装 " + newVer + "（当前 " + curVer + "）");
            if (Build.VERSION.SDK_INT >= 26 && !getPackageManager().canRequestPackageInstalls()) {
                toastUi("请允许「安装未知应用」，允许后会自动继续");
                try {
                    startActivity(new Intent(android.provider.Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                            Uri.parse("package:" + getPackageName())));
                } catch (Throwable ignored) {}
            }
            Uri uri = Uri.parse("content://" + APK_AUTHORITY + "/" + apk.getName());
            Intent it = new Intent(Intent.ACTION_VIEW);
            it.setDataAndType(uri, "application/vnd.android.package-archive");
            it.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(it);
            toastUi("下载完成，请在安装界面点「安装」");
        } catch (Throwable e) {
            toastUi("调起安装失败：" + (e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage()));
        }
    }

    /* ---------- 通知桥：前台消息落地通知 + 后台服务诊断 ---------- */
    class NotifyBridge {
        @JavascriptInterface
        public String status() {
            try {
                SharedPreferences p = PushService.prefs(MainActivity.this);
                int nmState = 1;
                try {
                    nmState = ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).areNotificationsEnabled() ? 1 : 0;
                } catch (Throwable ignored) {}
                long poll = p.getLong("last_poll", 0);
                return "{\"service\":" + (PushService.isRunning() ? "true" : "false")
                        + ",\"enabled\":" + (nmState == 1 ? "true" : "false")
                        + ",\"startedAt\":" + p.getLong("started_at", 0)
                        + ",\"lastPoll\":" + poll
                        + ",\"lastErr\":" + jsStr(p.getString("last_err", ""))
                        + ",\"dnd\":" + (p.getBoolean("dnd", false) ? "true" : "false") + "}";
            } catch (Throwable e) { return "{}"; }
        }

        @JavascriptInterface
        public void show(String title, String body, String conv) {
            if (!bridgeAllowed()) return;
            try {
                PushService.ensureChannels(MainActivity.this);
                NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                if (nm == null) return;
                SharedPreferences p = PushService.prefs(MainActivity.this);
                if (p.getBoolean("dnd", false)) return;
                if (Build.VERSION.SDK_INT >= 33 &&
                        checkSelfPermission("android.permission.POST_NOTIFICATIONS") != PackageManager.PERMISSION_GRANTED) return;
                Intent open = new Intent(MainActivity.this, MainActivity.class);
                open.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                open.putExtra("conv", conv == null ? "" : conv);
                PendingIntent pi = PendingIntent.getActivity(MainActivity.this, conv == null ? 0 : conv.hashCode(), open,
                        PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0));
                Notification n = new Notification.Builder(MainActivity.this,
                        Build.VERSION.SDK_INT >= 26 ? PushService.CH_MSG : null)
                        .setContentTitle(title == null ? "新消息" : title)
                        .setContentText(body == null ? "" : body)
                        .setStyle(new Notification.BigTextStyle().bigText(body == null ? "" : body))
                        .setSmallIcon(R.mipmap.ic_launcher)
                        .setAutoCancel(true)
                        .setContentIntent(pi)
                        .build();
                nm.notify(conv == null ? 4100 : conv.hashCode(), n);
            } catch (Throwable ignored) {}
        }

        @JavascriptInterface
        public void test() {
            if (!bridgeAllowed()) return;
            try {
                PushService.ensureChannels(MainActivity.this);
                android.app.NotificationManager nm =
                        (android.app.NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                if (nm == null) { toastUi("系统通知服务不可用"); return; }
                if (Build.VERSION.SDK_INT >= 33 &&
                        checkSelfPermission("android.permission.POST_NOTIFICATIONS") != PackageManager.PERMISSION_GRANTED) {
                    toastUi("还没给通知权限，请先点“设置”允许通知");
                    requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 1010);
                    return;
                }
                Notification n = new Notification.Builder(MainActivity.this,
                        Build.VERSION.SDK_INT >= 26 ? PushService.CH_MSG : null)
                        .setContentTitle("群栖：测试通知")
                        .setContentText("看到这条说明系统通知是通的")
                        .setSmallIcon(R.mipmap.ic_launcher)
                        .setAutoCancel(true)
                        .build();
                nm.notify(4200, n);
            } catch (Throwable e) {
                toastUi("测试通知失败：" + e.getClass().getSimpleName());
            }
        }

        @JavascriptInterface
        public void clear() {
            try {
                NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                if (nm != null) nm.cancelAll();
            } catch (Throwable ignored) {}
        }
    }
}
