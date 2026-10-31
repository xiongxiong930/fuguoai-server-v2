"""离线验证：固件 Media Thing 描述符 → 后端自动生成 media_* LLM 工具。

背景（契约 docs/plans/VOICE_MEDIA_INTEGRATION.md）：
后端 100% 描述符驱动——设备上报 descriptors，register_iot_tools 把每个 Thing 的
method 自动注册成 LLM 工具（命名 {thing小写}_{method小写}），LLM 调用时
_send_iot_command 把 {"type":"iot","commands":[{method}]} 经 WS 回注设备。

本测试不依赖真实设备/固件二进制：按固件 thing.h 的 GetDescriptorJson 序列化格式，
忠实构造 Media Thing 的 descriptor（方法名/参数类型与 main/iot/things/media.cc 一致），
喂进真实的 DeviceIoTExecutor，验证：
  1) 自动生成 7 个 media_* 工具；
  2) PlaySdMusic 参数 name 为 string、ShowSdPhoto 参数 index 为 number；
  3) 路由表正确 (control, Media, <Method>)；
  4) execute(media_openplayer / media_playsdmusic / media_showsdphoto) 下发的
     指令帧形状与后端现有 iot 通道完全一致（name=Media, method=原方法名,
     parameters 透传）。
脚本式运行（与 regression_gate L3 约定一致），全部通过打印 ALL CHECKS PASS，
任一失败非零退出。
"""
import asyncio
import json
import sys
from types import SimpleNamespace

# 让本测试可在仓库根目录以 `python tests/test_media_iot_tools.py` 运行
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fuguoai_core.providers.tools.device_iot.iot_executor import DeviceIoTExecutor


# ─────────────────────────────────────────────────────────
# 1) 按固件 thing.h GetDescriptorJson 格式，忠实构造 Media 描述符
#    （方法名/参数类型直接取自 main/iot/things/media.cc）
# ─────────────────────────────────────────────────────────
MEDIA_DESCRIPTOR = {
    "name": "Media",
    "description": ("本地媒体中心：音乐播放器与电子相册，读取设备 TF 卡(SD卡)的本地文件。"
                    "注意这是本机 TF 卡播放/浏览，不是服务器推流的音乐。"),
    "properties": {},
    "methods": {
        "OpenPlayer": {
            "description": "打开本地音乐播放器，显示 TF 卡(SD卡)里的歌曲列表，可播放/暂停、切歌",
            "parameters": {},
        },
        "OpenAlbum": {
            "description": "打开电子相册，浏览 TF 卡(SD卡)里的照片，可翻看",
            "parameters": {},
        },
        "PlaySdMusic": {
            "description": ("播放 TF 卡(SD卡)里的本地音乐；可指定歌名，不指定则播放/继续当前歌曲。"
                            "这是本地文件播放，不是服务器推流。"),
            "parameters": {
                "name": {"description": "歌曲名(可选，留空则播放当前或随机)", "type": "string"},
            },
        },
        "ShowSdPhoto": {
            "description": "显示 TF 卡(SD卡)相册里的第 N 张照片（N 从 1 开始）",
            "parameters": {
                "index": {"description": "照片序号，从 1 开始", "type": "number"},
            },
        },
        "Next": {
            "description": ("切换到下一首歌曲或下一张照片。注意这是本地 TF 卡(SD卡)播放/浏览的下一首，"
                            "不是服务器推流的音乐。"),
            "parameters": {},
        },
        "Prev": {
            "description": "切换到上一首歌曲或上一张照片。本地 TF 卡(SD卡)播放/浏览的上一首。",
            "parameters": {},
        },
        "Exit": {
            "description": "退出本地音乐播放器或电子相册，回到主菜单。",
            "parameters": {},
        },
    },
}

EXPECTED_TOOLS = {
    "media_openplayer", "media_openalbum", "media_playsdmusic",
    "media_showsdphoto", "media_next", "media_prev", "media_exit",
}

# 注：固件方法名应为 Next/Prev/Exit（去掉 Media 前缀），否则后端会生成
# 冗余的 media_medianext / media_mediaprev / media_mediaexit。本测试按干净命名断言。


class FakeConn:
    """最小替身：仅实现 execute 路径需要的 conn 接口。"""
    def __init__(self):
        self.iot_descriptors = {}
        self.sent = []

    async def safe_send(self, message):
        self.sent.append(message)


def _build_fake_conn_with_media():
    conn = FakeConn()
    methods = [{"name": m} for m in MEDIA_DESCRIPTOR["methods"].keys()]
    conn.iot_descriptors["Media"] = SimpleNamespace(methods=methods)
    return conn


async def _run_execute_and_capture(executor, conn, tool_name, arguments):
    conn.sent.clear()
    await executor.execute(conn, tool_name, arguments)
    assert conn.sent, f"{tool_name}: 未下发任何指令"
    return json.loads(conn.sent[-1])


def main():
    print("=== Media IoT 工具离线验证 ===")
    conn = _build_fake_conn_with_media()
    executor = DeviceIoTExecutor(conn)
    executor.register_iot_tools([MEDIA_DESCRIPTOR])
    tools = executor.get_tools()
    tool_names = set(tools.keys())

    # 检查 1：7 个工具全部生成
    missing = EXPECTED_TOOLS - tool_names
    assert not missing, f"缺失工具: {missing}"
    print(f"[1] 生成工具数={len(tool_names)} 含全部 7 个 media_* ✅")

    # 检查 2：参数类型正确
    sd_params = tools["media_playsdmusic"].description["function"]["parameters"]["properties"]
    assert sd_params["name"]["type"] == "string", f"PlaySdMusic.name 类型应为 string"
    photo_params = tools["media_showsdphoto"].description["function"]["parameters"]["properties"]
    assert photo_params["index"]["type"] == "number", f"ShowSdPhoto.index 类型应为 number"
    print("[2] PlaySdMusic.name=string / ShowSdPhoto.index=number ✅")

    # 检查 3：路由表正确（control, Media, <原方法名>）
    for method in MEDIA_DESCRIPTOR["methods"]:
        tool = f"media_{method.lower()}"
        kind, dev, sub = executor._tool_route[tool]
        assert kind == "control" and dev == "Media" and sub == method, \
            f"{tool} 路由异常: {executor._tool_route[tool]}"
    print("[3] 路由表 (control, Media, <原方法名>) 全部正确 ✅")

    # 检查 4：execute 下发帧形状（经事件循环）
    frame = asyncio.run(_run_execute_and_capture(
        executor, conn, "media_openplayer",
        {"response_success": "已打开播放器", "response_failure": "打开失败"}))
    assert frame == {"type": "iot", "commands": [{"name": "Media", "method": "OpenPlayer"}]}, \
        f"OpenPlayer 帧异常: {frame}"
    print(f"[4] media_openplayer 下发帧 ✅ {frame}")

    frame = asyncio.run(_run_execute_and_capture(
        executor, conn, "media_playsdmusic",
        {"name": "两只老虎", "response_success": "正在播放{value}", "response_failure": "失败"}))
    assert frame == {"type": "iot", "commands": [
        {"name": "Media", "method": "PlaySdMusic", "parameters": {"name": "两只老虎"}}]}, \
        f"PlaySdMusic 帧异常: {frame}"
    print(f"[4] media_playsdmusic(name=两只老虎) 下发帧 ✅ {frame}")

    frame = asyncio.run(_run_execute_and_capture(
        executor, conn, "media_showsdphoto",
        {"index": 3, "response_success": "已显示", "response_failure": "失败"}))
    assert frame == {"type": "iot", "commands": [
        {"name": "Media", "method": "ShowSdPhoto", "parameters": {"index": 3}}]}, \
        f"ShowSdPhoto 帧异常: {frame}"
    print(f"[4] media_showsdphoto(index=3) 下发帧 ✅ {frame}")

    print("\nALL CHECKS PASS ✅  后端零改动即可长出 7 个 media_* 工具，指令帧与现有 iot 通道一致。")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\n❌ FAIL: {e}")
        sys.exit(1)
    except Exception as e:  # noqa
        print(f"\n❌ ERROR: {e}")
        sys.exit(2)
