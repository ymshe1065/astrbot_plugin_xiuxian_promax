# -*- coding: utf-8 -*-
"""网页聊天室内置表情包目录。

图片来自百度贴吧经典表情系列（个人非商业使用），已随插件落在
``static/img/stickers/tieba/``，运行时不依赖外网。

前端只提交表情 id，服务端按白名单解析出图片地址后再广播，
避免客户端伪造任意 URL 注入聊天流。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

STICKER_DIR = Path(__file__).parent / "static" / "img" / "stickers" / "tieba"
STICKER_URL_PREFIX = "/static/img/stickers/tieba/"
GROUP_NAME = "百度贴吧"

# (id, 名称, 文件名) —— 顺序即前端表情面板的展示顺序
_STICKERS: tuple[tuple[str, str, str], ...] = (
    ("haha", "哈哈", "tieba_01_haha.png"),
    ("ok", "OK", "tieba_02_ok.png"),
    ("what", "what", "tieba_03_what.png"),
    ("bu_gaoxing", "不高兴", "tieba_04_bu_gaoxing.png"),
    ("guai", "乖", "tieba_05_guai.png"),
    ("ni_dong_de", "你懂的", "tieba_06_ni_dong_de.png"),
    ("bianbian", "便便", "tieba_07_bianbian.png"),
    ("mianqiang", "勉强", "tieba_08_mianqiang.png"),
    ("tushe", "吐舌", "tieba_09_tushe.png"),
    ("yamiedie", "呀咩爹", "tieba_10_yamiedie.png"),
    ("hehe", "呵呵", "tieba_11_hehe.png"),
    ("a", "啊", "tieba_12_a.png"),
    ("pen", "喷", "tieba_13_pen.png"),
    ("damuzhi", "大拇指", "tieba_14_damuzhi.png"),
    ("taikaixin", "太开心", "tieba_15_taikaixin.png"),
    ("taiyang", "太阳", "tieba_16_taiyang.png"),
    ("weiqu", "委屈", "tieba_17_weiqu.png"),
    ("xiaoguai", "小乖", "tieba_18_xiaoguai.png"),
    ("xiaohonglian", "小红脸", "tieba_19_xiaohonglian.png"),
    ("caihong", "彩虹", "tieba_20_caihong.png"),
    ("xinsui", "心碎", "tieba_21_xinsui.png"),
    ("nu", "怒", "tieba_22_nu.png"),
    ("jingku", "惊哭", "tieba_23_jingku.png"),
    ("jingya", "惊讶", "tieba_24_jingya.png"),
    ("landeli", "懒得理", "tieba_25_landeli.png"),
    ("shouzhi", "手纸", "tieba_26_shouzhi.png"),
    ("wabi", "挖鼻", "tieba_27_wabi.png"),
    ("wuzuixiao", "捂嘴笑", "tieba_28_wuzuixiao.png"),
    ("xingxingyueliang", "星星月亮", "tieba_29_xingxingyueliang.png"),
    ("han", "汗", "tieba_30_han.png"),
    ("shafa", "沙发", "tieba_31_shafa.png"),
    ("lei", "泪", "tieba_32_lei.png"),
    ("huaji", "滑稽", "tieba_33_huaji.png"),
    ("dengpao", "灯泡", "tieba_34_dengpao.png"),
    ("aixin", "爱心", "tieba_35_aixin.png"),
    ("xili", "犀利", "tieba_36_xili.png"),
    ("kuanghan", "狂汗", "tieba_37_kuanghan.png"),
    ("meigui", "玫瑰", "tieba_38_meigui.png"),
    ("yiwen", "疑问", "tieba_39_yiwen.png"),
    ("zhenbang", "真棒", "tieba_40_zhenbang.png"),
    ("shuijiao", "睡觉", "tieba_41_shuijiao.png"),
    ("liwu", "礼物", "tieba_42_liwu.png"),
    ("xiaoniao", "笑尿", "tieba_43_xiaoniao.png"),
    ("xiaoyan", "笑眼", "tieba_44_xiaoyan.png"),
    ("honglingjin", "红领巾", "tieba_45_honglingjin.png"),
    ("shengli", "胜利", "tieba_46_shengli.png"),
    ("huaxin", "花心", "tieba_47_huaxin.png"),
    ("chabei", "茶杯", "tieba_48_chabei.png"),
    ("yaowan", "药丸", "tieba_49_yaowan.png"),
    ("danggao", "蛋糕", "tieba_50_danggao.png"),
    ("lazhu", "蜡烛", "tieba_51_lazhu.png"),
    ("bishi", "鄙视", "tieba_52_bishi.png"),
    ("ku", "酷", "tieba_53_ku.png"),
    ("suanshuang", "酸爽", "tieba_54_suanshuang.png"),
    ("qianbi", "钱币", "tieba_55_qianbi.png"),
    ("yinxian", "阴险", "tieba_56_yinxian.png"),
    ("yinyue", "音乐", "tieba_57_yinyue.png"),
    ("xiangjiao", "香蕉", "tieba_58_xiangjiao.png"),
    ("heixian", "黑线", "tieba_59_heixian.png"),
    ("tu", "吐", "tieba_60_tu.png"),
)

STICKERS: tuple[dict[str, str], ...] = tuple(
    {"id": sid, "label": label, "file": filename}
    for sid, label, filename in _STICKERS
)

_BY_ID: dict[str, dict[str, str]] = {item["id"]: item for item in STICKERS}
_BY_LABEL: dict[str, dict[str, str]] = {item["label"]: item for item in STICKERS}


def _public(item: dict[str, str]) -> dict[str, str]:
    """供前端/广播使用的对外结构（含可访问地址）。"""
    return {
        "id": item["id"],
        "label": item["label"],
        "url": STICKER_URL_PREFIX + item["file"],
    }


def catalog() -> list[dict[str, str]]:
    """返回全部内置贴吧表情。"""
    return [_public(item) for item in STICKERS]


def groups() -> list[dict[str, Any]]:
    """按系列分组，便于以后继续扩充其它系列。"""
    return [{"name": GROUP_NAME, "stickers": catalog()}]


def get(sticker_id: str) -> dict[str, str] | None:
    """按 id 取表情（对外结构）；不存在返回 None。"""
    item = _BY_ID.get(str(sticker_id or "").strip())
    return _public(item) if item else None


def find_by_label(label: str) -> dict[str, str] | None:
    """按贴吧快捷写法 ``#(滑稽)`` 里的名称取表情。"""
    item = _BY_LABEL.get(str(label or "").strip())
    return _public(item) if item else None
