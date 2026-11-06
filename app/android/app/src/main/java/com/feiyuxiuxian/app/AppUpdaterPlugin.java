package com.feiyuxiuxian.app;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;

import androidx.core.content.FileProvider;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;

/**
 * 应用内更新：下载 APK 并交给系统安装器。
 *
 * <p>Android 不允许第三方应用静默安装（除非系统签名 / Device Owner / root），所以这里能做到
 * 的上限就是「替用户下载 + 拉起安装器」，最后一步「安装」必须由用户点确认。
 *
 * <p>为什么不用 DownloadManager：
 * DownloadManager 跑在系统进程里，明文 HTTP 的放行策略与当前 App 的
 * {@code android:usesCleartextTraffic="true"} 不一定一致，容易在 http:// 站点上直接失败。
 * 用 HttpURLConnection 走 App 自己的网络栈，行为可预期，还能顺带算 sha256 和报进度。
 */
@CapacitorPlugin(name = "AppUpdater")
public class AppUpdaterPlugin extends Plugin {

    private static final String DIR_NAME = "updates";
    private static final int CONNECT_TIMEOUT = 15000;
    private static final int READ_TIMEOUT = 30000;

    /** 下载 APK 到 cacheDir/updates，边下边发 progress 事件。 */
    @PluginMethod
    public void download(PluginCall call) {
        final String url = call.getString("url");
        final String fileName = sanitize(call.getString("fileName", "update.apk"));
        final String expectSha = normalizeSha(call.getString("sha256", ""));

        if (url == null || url.isEmpty()) {
            call.reject("缺少下载地址");
            return;
        }
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            // 只放行网络地址，防止被构造成 file:// / content:// 读取本地文件
            call.reject("不支持的下载地址");
            return;
        }

        final File dir = new File(getContext().getCacheDir(), DIR_NAME);
        if (!dir.isDirectory() && !dir.mkdirs()) {
            call.reject("无法创建下载目录");
            return;
        }
        final File target = new File(dir, fileName);

        // 同名同摘要的文件已经下过就直接复用，重试时不必再走一遍流量
        if (target.isFile() && target.length() > 0
                && (expectSha.isEmpty() || expectSha.equals(sha256Of(target)))) {
            JSObject ret = new JSObject();
            ret.put("path", target.getAbsolutePath());
            ret.put("size", target.length());
            call.resolve(ret);
            return;
        }

        new Thread(() -> {
            HttpURLConnection conn = null;
            File part = new File(dir, fileName + ".part");
            try {
                conn = (HttpURLConnection) new URL(url).openConnection();
                conn.setConnectTimeout(CONNECT_TIMEOUT);
                conn.setReadTimeout(READ_TIMEOUT);
                conn.setInstanceFollowRedirects(true);
                // 关掉压缩，Content-Length 才是真实字节数，进度条与总量才对得上
                conn.setRequestProperty("Accept-Encoding", "identity");
                conn.connect();

                int code = conn.getResponseCode();
                if (code != HttpURLConnection.HTTP_OK) {
                    call.reject("下载失败（HTTP " + code + "）");
                    return;
                }
                long total = parseLong(conn.getHeaderField("Content-Length"));
                if (total <= 0) {
                    total = parseLong(conn.getHeaderField("Content-Range"));
                }

                MessageDigest digest = expectSha.isEmpty() ? null : MessageDigest.getInstance("SHA-256");
                long received = 0;
                long lastNotify = 0;
                int lastPercent = -1;
                byte[] buf = new byte[32 * 1024];

                try (InputStream in = conn.getInputStream();
                     FileOutputStream out = new FileOutputStream(part)) {
                    int n;
                    while ((n = in.read(buf)) != -1) {
                        out.write(buf, 0, n);
                        received += n;
                        if (digest != null) {
                            digest.update(buf, 0, n);
                        }
                        int percent = total > 0 ? (int) (received * 100 / total) : 0;
                        long now = System.currentTimeMillis();
                        // 每 1% 或 200ms 报一次，避免高频事件把 WebView 压住
                        if (percent != lastPercent || now - lastNotify > 200) {
                            lastPercent = percent;
                            lastNotify = now;
                            emitProgress(percent, received, total);
                        }
                    }
                }

                if (total > 0 && received != total) {
                    part.delete();
                    call.reject("下载不完整（" + received + "/" + total + "）");
                    return;
                }
                if (digest != null) {
                    String actual = toHex(digest.digest());
                    if (!actual.equalsIgnoreCase(expectSha)) {
                        part.delete();
                        call.reject("安装包校验失败，请重试");
                        return;
                    }
                }

                if (target.exists() && !target.delete()) {
                    part.delete();
                    call.reject("无法覆盖旧安装包");
                    return;
                }
                if (!part.renameTo(target)) {
                    part.delete();
                    call.reject("保存安装包失败");
                    return;
                }

                JSObject ret = new JSObject();
                ret.put("path", target.getAbsolutePath());
                ret.put("size", received);
                call.resolve(ret);
            } catch (Exception ex) {
                part.delete();
                call.reject("下载出错：" + ex.getMessage());
            } finally {
                if (conn != null) {
                    conn.disconnect();
                }
            }
        }).start();
    }

    /** 调起系统安装器；没开「安装未知应用」权限就先跳设置页。 */
    @PluginMethod
    public void install(PluginCall call) {
        String path = call.getString("path");
        if (path == null || path.isEmpty()) {
            call.reject("缺少安装包路径");
            return;
        }
        File file = new File(path);
        if (!file.isFile()) {
            call.reject("安装包不存在，请重新下载");
            return;
        }
        Activity activity = getActivity();
        if (activity == null) {
            call.reject("当前没有可用的界面");
            return;
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                && !activity.getPackageManager().canRequestPackageInstalls()) {
            try {
                Intent settings = new Intent(
                        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse("package:" + activity.getPackageName()));
                settings.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                activity.startActivity(settings);
            } catch (Exception ignored) {
                // 部分 ROM 没有这个页面，直接让安装器自己引导
            }
            call.reject("需要允许本应用安装应用", "NEED_PERMISSION");
            return;
        }

        try {
            Uri uri = FileProvider.getUriForFile(
                    activity, activity.getPackageName() + ".fileprovider", file);
            Intent intent = new Intent(Intent.ACTION_VIEW);
            intent.setDataAndType(uri, "application/vnd.android.package-archive");
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
            activity.startActivity(intent);
            call.resolve();
        } catch (Exception ex) {
            call.reject("无法调起安装器：" + ex.getMessage());
        }
    }

    // ---------- 工具 ----------

    private void emitProgress(int percent, long received, long total) {
        final JSObject data = new JSObject();
        data.put("percent", percent);
        data.put("received", received);
        data.put("total", total);
        Activity activity = getActivity();
        if (activity != null) {
            activity.runOnUiThread(() -> notifyListeners("progress", data));
        }
    }

    /** 只保留文件名本身，防住 ../ 之类的路径穿越。 */
    private static String sanitize(String name) {
        String base = name == null ? "" : name;
        int slash = Math.max(base.lastIndexOf('/'), base.lastIndexOf('\\'));
        if (slash >= 0) {
            base = base.substring(slash + 1);
        }
        base = base.replace("..", "").trim();
        if (base.isEmpty()) {
            base = "update.apk";
        }
        return base;
    }

    private static String normalizeSha(String sha) {
        String s = sha == null ? "" : sha.trim().toLowerCase();
        return s.length() == 64 ? s : "";
    }

    private static long parseLong(String raw) {
        if (raw == null) {
            return 0;
        }
        try {
            return Long.parseLong(raw.trim());
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    private static String sha256Of(File file) {
        try (InputStream in = new java.io.FileInputStream(file)) {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] buf = new byte[64 * 1024];
            int n;
            while ((n = in.read(buf)) != -1) {
                digest.update(buf, 0, n);
            }
            return toHex(digest.digest());
        } catch (Exception e) {
            return "";
        }
    }

    private static String toHex(byte[] bytes) {
        StringBuilder sb = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) {
            sb.append(Character.forDigit((b >> 4) & 0xF, 16));
            sb.append(Character.forDigit(b & 0xF, 16));
        }
        return sb.toString();
    }
}
