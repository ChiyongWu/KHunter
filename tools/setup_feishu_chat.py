# -*- coding: utf-8 -*-
"""
飞书 chat_id 配置助手

帮助配置飞书通知所需的目标群聊 ID。

使用方法:
    python tools/setup_feishu_chat.py

支持两种方式获取 chat_id:
  1. 自动发现 - 调用飞书 API 列出 bot 所在的群聊
  2. 手动输入 - 从飞书群聊 URL 中复制
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from scheduler.notifier import FeishuNotifier


def load_config():
    """加载配置"""
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "config.yaml"
    )
    with open(config_path, "r", encoding="utf-8") as f:
        return config_path, yaml.safe_load(f)


def save_chat_id(config_path, config, chat_id):
    """保存 chat_id 到配置文件"""
    config["feishu"]["chat_id"] = chat_id
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    print(f"\n    [OK] chat_id 已保存到 config.yaml")


def automatic_discovery(notifier):
    """方式1: 自动发现"""
    print("\n[1] 自动发现模式")
    print("    正在查询 bot 所在的群聊列表...")
    print("    (需要 bot 已添加到目标群，且应用有 im:chat 权限)")
    print()

    chat_id = notifier.get_chat_id()
    if chat_id:
        return chat_id
    else:
        print("    [WARN] 未能自动获取 chat_id")
        return None


def manual_input():
    """方式2: 手动输入"""
    print("\n[2] 手动输入模式")
    print("    " + "=" * 52)
    print("    获取 chat_id 的方法：")
    print()
    print("    A. 飞书桌面端：")
    print("       1. 打开目标群聊")
    print("       2. 点击右上角群设置 > 群机器人")
    print("       3. 找到你的 bot，点击进入详情")
    print("       4. chat_id 格式: oc_xxxxxxxxxxxxxxxx")
    print()
    print("    B. 飞书网页版：")
    print("       1. 打开 https://feishu.cn/messenger/")
    print("       2. 进入目标群聊")
    print("       3. URL 中的 chat_id 参数即为群 ID")
    print("       例如: .../chat/oc_5b6a3e8c9d0f1234...")
    print("    " + "=" * 52)
    print()

    chat_id = input("    请输入 chat_id (直接回车跳过): ").strip()
    return chat_id if chat_id else None


def verify_send(notifier, chat_id):
    """验证：发送测试消息"""
    print("\n[3] 发送测试消息验证...")

    from scheduler.models import PipelineResult, StepResult
    from datetime import datetime

    result = PipelineResult(
        start_time=datetime.now(),
        end_time=datetime.now(),
        duration_seconds=0.5,
        status="success",
    )
    result.add_step(StepResult(
        step_name="data_update",
        status="success",
        start_time=datetime.now(),
        end_time=datetime.now(),
        duration_seconds=0.3,
        details={"kline_updated": 1568, "exdividend_rebuilt": 3, "basic_data_synced": True},
    ))
    result.add_step(StepResult(
        step_name="strategy_run",
        status="success",
        start_time=datetime.now(),
        end_time=datetime.now(),
        duration_seconds=0.1,
        details={"buy_signals": 3, "sell_signals": 1},
    ))

    notifier.chat_id = chat_id
    success = notifier.send_summary(result)
    if success:
        print("    [OK] 测试消息发送成功！请检查飞书群。")
        return True
    else:
        print("    [FAIL] 消息发送失败，请检查 chat_id 是否正确")
        return False


def main():
    print("=" * 60)
    print("Feishu Chat ID Setup")
    print("=" * 60)

    # 检查前置条件
    print("\n[0] 前置条件检查")
    print("    1. 飞书应用已创建并发布")
    print("    2. 应用添加了 im:message 权限")
    print("    3. 在飞书群设置 > 群机器人中添加了你的 bot")
    print()

    config_path, config = load_config()
    notifier = FeishuNotifier.from_config(config)

    if not notifier.enabled:
        print("    [ERROR] 飞书配置无效，请检查 config.yaml 中 feishu 段")
        return 1

    current_chat_id = config.get("feishu", {}).get("chat_id", "")
    if current_chat_id:
        print(f"    当前 chat_id: {current_chat_id}")
    else:
        print("    当前 chat_id: (未配置)")

    # 尝试自动发现
    chat_id = automatic_discovery(notifier)

    # 自动发现失败，尝试手动输入
    if not chat_id:
        chat_id = manual_input()

    if not chat_id:
        print("\n    [SKIP] 未获取到 chat_id，配置保持不变")
        print("    请先将 bot 添加到飞书群后再运行此脚本")
        return 0

    # 保存配置
    save_chat_id(config_path, config, chat_id)

    # 验证发送
    verify_send(notifier, chat_id)

    print(f"\n{'=' * 60}")
    print("Setup complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
