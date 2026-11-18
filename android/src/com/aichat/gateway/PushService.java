package com.aichat.gateway;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;

public class PushService extends Service {
    static final String CH_SVC = "qunqi_service";
    static final String CH_MSG = "qunqi_msg";
    static final int ID_SVC = 9001;
    private static final int INTERVAL = 15000;
    private static final int MAX_INTERVAL = 120000;
    private volatile boolean running = false;
    private static volatile boolean alive = false;
    private Thread worker;

    static boolean isRunning() { return alive; }

    static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences("push", Context.MODE_PRIVATE);
    }

    static void ensureChannels(Context c) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) return;
        NotificationChannel svc = new NotificationChannel(CH_SVC, "后台消息同步", NotificationManager.IMPORTANCE_LOW);
        svc.setShowBadge(false);
        NotificationChannel msg = new NotificationChannel(CH_MSG, "新消息提醒", NotificationManager.IMPORTANCE_HIGH);
        msg.enableVibration(true);
        nm.createNotificationChannel(svc);
        nm.createNotificationChannel(msg);
    }

    /* 每 15 分钟一个闹钟；服务被系统清掉也能靠它复活，不再依赖 App 是否被打开 */
    static void scheduleWatchdog(Context c) {
        try {
            android.app.AlarmManager am = (android.app.AlarmManager) c.getSystemService(Context.ALARM_SERVICE);
            if (am == null) return;
            Intent i = new Intent(c, WatchdogReceiver.class);
            PendingIntent pi = PendingIntent.getBroadcast(c, 7001, i,
                    PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0));
            am.setInexactRepeating(android.app.AlarmManager.ELAPSED_REALTIME_WAKEUP,
                    android.os.SystemClock.elapsedRealtime() + 5 * 60 * 1000L, 15 * 60 * 1000L, pi);
        } catch (Throwable ignored) {}
    }

    static void startIfConfigured(Context c) {
        SharedPreferences p = prefs(c);
        String dev = p.getString("device", "");
        String base = p.getString("base", "");
        if (dev != null && dev.length() > 0 && base != null && base.length() > 0) {
            Intent i = new Intent(c, PushService.class);
            if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i); else c.startService(i);
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        ensureChannels(this);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification n = new Notification.Builder(this, Build.VERSION.SDK_INT >= 26 ? CH_SVC : null)
                .setContentTitle("群栖正在后台接收消息")
                .setContentText("有新消息会通知你")
                .setSmallIcon(R.mipmap.ic_launcher)
                .setOngoing(true)
                .build();
        try { startForeground(ID_SVC, n); } catch (Throwable ignored) {}
        alive = true;
        try {
            if (prefs(this).getLong("started_at", 0) == 0)
                prefs(this).edit().putLong("started_at", System.currentTimeMillis()).apply();
        } catch (Throwable ignored) {}
        if (!running) { running = true; startWorker(); }
        scheduleWatchdog(this);
        return START_STICKY;
    }


    private void reportHb(String err) {
        try {
            SharedPreferences p = prefs(this);
            String base = p.getString("base", "");
            String dev = p.getString("device", "");
            String sec = p.getString("secret", "");
            if (base.length() == 0 || dev.length() == 0) return;
            JSONObject body = new JSONObject();
            body.put("alive", true);
            body.put("lastPoll", p.getLong("last_poll", 0));
            body.put("startedAt", p.getLong("started_at", 0));
            body.put("lastErr", err == null ? "" : err);
            HttpURLConnection c = (HttpURLConnection) new URL(
                    base.replaceAll("/+$", "") + "/api/push/heartbeat").openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(8000);
            c.setReadTimeout(8000);
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            if (sec.length() > 0) c.setRequestProperty("X-Device-Secret", sec);
            c.setRequestProperty("X-Device", dev);
            c.getOutputStream().write(body.toString().getBytes("UTF-8"));
            c.getResponseCode();
            c.disconnect();
        } catch (Throwable ignored) {}
    }

    private void startWorker() {
        worker = new Thread(() -> {
            reportHb("start");
            int wait = INTERVAL;
            boolean hadNews = false;
            while (running) {
                SharedPreferences p = prefs(this);
                try {
                    String dev = p.getString("device", "");
                    String base = p.getString("base", "");
                    String secret = p.getString("secret", "");
                    long since = p.getLong("since", 0);
                    if (dev.length() == 0 || base.length() == 0) {
                        running = false;
                        break;
                    }
                    String u = base.replaceAll("/+$", "") + "/api/push/poll?device=" + URLEncoder.encode(dev, "UTF-8") + "&since=" + since;
                    HttpURLConnection c = (HttpURLConnection) new URL(u).openConnection();
                    c.setConnectTimeout(8000);
                    c.setReadTimeout(8000);
                    if (secret.length() > 0) c.setRequestProperty("X-Device-Secret", secret);
                    int code = c.getResponseCode();
                    if (code == 401 || code == 403) {
                        c.disconnect();
                        notifyRelogin();
                        running = false;
                        break;
                    }
                    BufferedReader br = new BufferedReader(new InputStreamReader(code >= 400 ? c.getErrorStream() : c.getInputStream(), "UTF-8"));
                    StringBuilder sb = new StringBuilder();
                    String line;
                    while ((line = br.readLine()) != null) sb.append(line);
                    br.close();
                    c.disconnect();
                    JSONObject d = new JSONObject(sb.toString());
                    JSONArray arr = d.optJSONArray("items");
                    long mx = d.optLong("max_ts", since);
                    if (mx > 0) p.edit().putLong("since", mx).apply();
                    hadNews = (arr != null && arr.length() > 0);
                    boolean dnd = p.getBoolean("dnd", false);
                    p.edit().putLong("last_poll", System.currentTimeMillis()).putString("last_err", "").apply();
                    reportHb("");
                    if (hadNews && !MainActivity.isVisible() && !dnd) {
                        for (int i = Math.max(0, arr.length() - 3); i < arr.length(); i++) {
                            JSONObject m = arr.getJSONObject(i);
                            notifyMsg(m.optString("conv_name", "新消息"), m.optString("text", ""), m.optString("conv", ""));
                        }
                    }
                    if (hadNews) wait = INTERVAL;
                } catch (Throwable e) {
                    hadNews = false;
                    try {
                        prefs(this).edit().putString("last_err",
                                e.getClass().getSimpleName() + ": " + (e.getMessage() == null ? "" : e.getMessage())).apply();
                        reportHb(e.getClass().getSimpleName() + ": " + (e.getMessage() == null ? "" : e.getMessage()));
                    } catch (Throwable ignored) {}
                }
                if (!hadNews) wait = Math.min(wait * 2, MAX_INTERVAL);
                try { Thread.sleep(wait); } catch (InterruptedException e) { break; }
            }
        }, "qunqi-push");
        worker.setDaemon(true);
        worker.start();
    }

    private void notifyRelogin() {
        try {
            NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            Notification n = new Notification.Builder(this, Build.VERSION.SDK_INT >= 26 ? CH_MSG : null)
                    .setContentTitle("群栖：登录已失效")
                    .setContentText("打开 App 输入身份码恢复登录，否则收不到新消息")
                    .setSmallIcon(R.mipmap.ic_launcher)
                    .setAutoCancel(true)
                    .build();
            nm.notify(4099, n);
        } catch (Throwable ignored) {}
    }

    private void notifyMsg(String title, String body, String conv) {
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        if (conv.length() > 0) open.putExtra("conv", conv);
        PendingIntent pi = PendingIntent.getActivity(this, conv.hashCode(), open,
                PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0));
        Notification n = new Notification.Builder(this, Build.VERSION.SDK_INT >= 26 ? CH_MSG : null)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setSmallIcon(R.mipmap.ic_launcher)
                .setAutoCancel(true)
                .setContentIntent(pi)
                .build();
        nm.notify(conv.hashCode(), n);
    }

    @Override
    public void onDestroy() {
        reportHb("destroyed");
        running = false;
        alive = false;
        try { prefs(this).edit().putLong("started_at", 0).apply(); } catch (Throwable ignored) {}
        if (worker != null) worker.interrupt();
        super.onDestroy();
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        /* 用户从最近任务里划掉 App：服务重新起来，别让通知断掉 */
        try {
            Intent again = new Intent(this, PushService.class);
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(again); else startService(again);
        } catch (Throwable ignored) {}
        scheduleWatchdog(this);
        super.onTaskRemoved(rootIntent);
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }
}
