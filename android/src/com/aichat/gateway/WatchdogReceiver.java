package com.aichat.gateway;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/* 闹钟兜底：国产 ROM 常把前台服务直接清掉，靠 AlarmManager 定期把它重新拉起并继续轮询 */
public class WatchdogReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        PushService.startIfConfigured(context);
        PushService.scheduleWatchdog(context);
    }
}
