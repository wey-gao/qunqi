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
    private volatile boolean running = false;
    private Thread worker;

    static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences("push", Context.MODE_PRIVATE);
    }

    static void ensureChannels(Context c) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) return;
        NotificationChannel svc = new NotificationChannel(CH_SVC, "后台消息同步", NotificationManager.IMPORTANCE_MIN);
        svc.setShowBadge(false);
        NotificationChannel msg = new NotificationChannel(CH_MSG, "新消息提醒", NotificationManager.IMPORTANCE_HIGH);
        msg.enableVibration(true);
        nm.createNotificationChannel(svc);
        nm.createNotificationChannel(msg);
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
        startForeground(ID_SVC, n);
        if (!running) { running = true; startWorker(); }
        return START_STICKY;
    }

    private void startWorker() {
        worker = new Thread(() -> {
            SharedPreferences p = prefs(this);
            while (running) {
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
                    BufferedReader br = new BufferedReader(new InputStreamReader(c.getInputStream(), "UTF-8"));
                    StringBuilder sb = new StringBuilder();
                    String line;
                    while ((line = br.readLine()) != null) sb.append(line);
                    br.close();
                    c.disconnect();
                    JSONObject d = new JSONObject(sb.toString());
                    JSONArray arr = d.optJSONArray("items");
                    long mx = d.optLong("max_ts", since);
                    if (mx > 0) p.edit().putLong("since", mx).apply();
                    if (arr != null && arr.length() > 0 && !MainActivity.isVisible()) {
                        for (int i = Math.max(0, arr.length() - 3); i < arr.length(); i++) {
                            JSONObject m = arr.getJSONObject(i);
                            notifyMsg(m.optString("conv_name", "新消息"), m.optString("text", ""), m.optString("conv", ""));
                        }
                    }
                } catch (Throwable ignored) {}
                try { Thread.sleep(INTERVAL); } catch (InterruptedException e) { break; }
            }
        }, "qunqi-push");
        worker.setDaemon(true);
        worker.start();
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
        running = false;
        if (worker != null) worker.interrupt();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }
}
