package com.feiyuxiuxian.app;

import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.View;
import android.webkit.WebView;

import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    /**
     * 状态栏区域填充色。
     *
     * 与 splash 背景、index.html 的 theme-color 一致，切换主题时这块留白不会跳色。
     * 若以后主题色改动，这里同步改一下即可。
     */
    private static final int BAR_COLOR = Color.parseColor("#eef4ff");

    @Override
    public void onCreate(Bundle savedInstanceState) {
        // targetSdk ≥ 35 起系统强制 edge-to-edge，这里显式声明一次，
        // 让 Android 15 与更低版本走同一套 insets 分发逻辑
        WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
        super.onCreate(savedInstanceState);
        applySystemBarInsets();
    }

    /**
     * 把状态栏 / 导航栏 / 刘海的位置让给 WebView 内容。
     *
     * 背景：Android 15 起状态栏是透明浮层——应用窗口铺满全屏，电量、信号等图标
     * 直接画在页面之上，不再占布局空间。不处理的话网页顶栏就被图标压住。
     *
     * 做法：给内容根布局加上与系统栏等高的 padding，并把这块区域填成顶栏同色，
     * 视觉上就和「有状态栏」一样（QQ 等应用的常规做法）。
     *
     * 两个细节：
     * 1) 只吃 systemBars + displayCutout，不吃 ime()——键盘弹出时不重复让位；
     * 2) 返回 CONSUMED，阻断 insets 继续下发给 WebView。否则 WebView 里
     *    env(safe-area-inset-top) 也会拿到状态栏高度，和这里的 padding 叠加，
     *    顶部会多出一倍空白。
     */
    private void applySystemBarInsets() {
        final View content = findViewById(android.R.id.content);
        if (content == null) {
            return;
        }
        getWindow().setBackgroundDrawable(new ColorDrawable(BAR_COLOR));
        content.setBackgroundColor(BAR_COLOR);
        ViewCompat.setOnApplyWindowInsetsListener(content, (view, windowInsets) -> {
            Insets bars = windowInsets.getInsets(
                    WindowInsetsCompat.Type.systemBars()
                            | WindowInsetsCompat.Type.displayCutout());
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            return WindowInsetsCompat.CONSUMED;
        });
        ViewCompat.requestApplyInsets(content);
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            WebView webView = getBridge().getWebView();
            if (webView != null && webView.canGoBack()) {
                webView.goBack();
                return true;
            }
        }
        return super.onKeyDown(keyCode, event);
    }
}
