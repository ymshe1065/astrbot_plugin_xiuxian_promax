package com.feiyuxiuxian.app;

import android.os.Bundle;
import android.view.KeyEvent;
import android.webkit.WebView;

import com.getcapacitor.BridgeActivity;
import com.getcapacitor.BridgeWebViewClient;
import com.getcapacitor.PluginHandle;

import java.util.List;

public class MainActivity extends BridgeActivity {

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // 让 WebView 处理自身的页面内导航（返回键优先回退网页历史）
        BridgeWebViewClient client = new BridgeWebViewClient(this.bridge) {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                // 保持在同一 WebView 内加载，不跳系统浏览器
                return false;
            }
        };
        // bridge 已由父类初始化，此处仅做兜底，不做额外绑定
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
