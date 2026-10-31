# -*- coding: utf-8 -*-
"""复果AI · 启动前配置体检

仅做一件事：确认 ``data/.config.yaml`` 存在，且"走管理端拉配置"与"本地
selected_module"两种模式没有混写在同一份文件里。体检通过后置位进程级标记，
同进程内重复调用直接短路。
"""

import asyncio
import os

from fuguoai_config.config_loader import get_project_dir, load_config, read_config

default_config_file = "config.yaml"
config_file_valid = False


def check_config_file():
    global config_file_valid
    if config_file_valid:
        return

    user_cfg_path = get_project_dir() + "data/." + default_config_file
    if not os.path.exists(user_cfg_path):
        raise FileNotFoundError(
            "找不到data/.config.yaml文件，请按教程确认该配置文件是否存在"
        )

    # 若配置声明走管理端接口，则本地文件里不应再残留 selected_module（两种模式互斥）
    config = asyncio.run(load_config())
    if config.get("read_config_from_api", False):
        print("从API读取配置")
        raw_user_cfg = read_config(user_cfg_path)
        if raw_user_cfg.get("selected_module") is not None:
            raise ValueError(
                "您的配置文件好像既包含智控台的配置又包含本地配置：\n"
                "\n建议您：\n"
                "1、将根目录的config_from_api.yaml文件复制到data下，重命名为.config.yaml\n"
                "2、按教程配置好接口地址和密钥\n"
            )

    config_file_valid = True
