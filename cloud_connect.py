#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paste the cloud's AMC1 connection information in a local terminal.

This entry point reads the same payload as the web panel and Ubuntu installer.
It never imports server, scans miners, or opens the database. It tests the new
connection before saving, and keeps the previous binding on failure.
"""
import argparse
import base64
import binascii
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import warnings
from urllib.parse import urlsplit

import yaml

import appconfig
import cloud_report

SITE_TYPES = ("air", "hydro", "mixed")
MAX_CODE_LENGTH = 16384
IS_WINDOWS = os.name == "nt"


class Terminal:
    def write(self, text):
        print(text, flush=True)

    def ask(self, text, secret=False):
        if secret:
            if not sys.stdin.isatty():
                raise SetupError("请在终端粘贴接入信息；自动化调用可使用 --stdin 从标准输入传入。")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    return getpass.getpass(text).strip()
            except getpass.GetPassWarning:
                raise SetupError("此终端无法隐藏输入，请使用普通命令窗口，或使用本地网页粘贴连接。") from None
        return input(text).strip()


class SetupError(ValueError):
    pass


def site_key(value):
    """与云端 validate.site_key 一致，仅用于校验，不改写身份；允许中文及中间空格。"""
    if value is None:
        return ""
    text = "".join(ch for ch in str(value) if ch >= " " and ch != "\x7f").strip()[:65]
    return text if 0 < len(text) <= 64 else ""


def decode_code(code):
    code = str(code or "").strip()
    if len(code) > MAX_CODE_LENGTH or not re.fullmatch(r"AMC1-[A-Za-z0-9_-]+", code):
        raise SetupError("接入信息格式不对，应以 AMC1- 开头")
    try:
        encoded = code[5:]
        data = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4),
                                          altchars=b"-_", validate=True))
    except (ValueError, UnicodeError, binascii.Error):
        raise SetupError("接入信息解不开，请从云端完整复制") from None
    if not isinstance(data, dict):
        raise SetupError("接入信息内容不完整")
    if not isinstance(data.get("u"), str) or not isinstance(data.get("t"), str):
        raise SetupError("接入信息缺少云端地址或上报凭据")
    url, token = validate_url(data["u"]), data["t"].strip()
    if not token or any(ord(ch) < 32 or ord(ch) == 127 for ch in token):
        raise SetupError("接入信息中的上报凭据格式不对，请重新复制")
    for field in ("f", "s", "n", "y"):
        if data.get(field) is not None and not isinstance(data[field], str):
            raise SetupError("接入信息中的字段格式不对，请从云端完整复制")
    fp = cloud_report.normalize_fingerprint(data.get("f"))
    if fp is None:
        raise SetupError("接入信息中的证书指纹格式不对")
    if data.get("s") and site_key(data["s"]) != data["s"]:
        raise SetupError("接入信息中的场地 ID 格式不对")
    if data.get("n") and (len(data["n"].strip()) > 64 or any(ord(ch) < 32 or ord(ch) == 127 for ch in data["n"])):
        raise SetupError("接入信息中的场地名称格式不对")
    if data.get("y") is not None and data["y"] not in SITE_TYPES:
        raise SetupError("接入信息中的场地类型须为 air、hydro 或 mixed")
    data.update(u=url, t=token, f=fp)
    return data


def validate_url(value):
    """Shared terminal/web URL rules; reject credentials and ambiguous endpoints."""
    url = str(value or "").strip().rstrip("/")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise SetupError("云端上报地址格式不对")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        parsed.port  # 校验端口，避免保存无法使用的地址
    except ValueError:
        raise SetupError("接入信息中的云端地址格式不对") from None
    if (parsed.scheme not in ("http", "https") or not host or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise SetupError("云端上报地址须为 http:// 或 https:// 地址，不能包含账号密码、查询参数或片段")
    if parsed.scheme == "http":
        try:
            private = ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
        except ValueError:
            private = host == "localhost"
        if not private:
            raise SetupError("公网云端必须使用 HTTPS，请从云端重新复制接入信息")
    return parsed.geturl().rstrip("/")


def read_state():
    # load_settings() 会容忍损坏文件并回退为空；绑定必须严格读，避免丢失原配置。
    try:
        with open(appconfig.SETTINGS_FILE, encoding="utf-8") as source:
            saved = json.load(source)
    except FileNotFoundError:
        saved = {}
    if not isinstance(saved, dict) or ("cloud" in saved and not isinstance(saved["cloud"], dict)):
        raise SetupError("现有 settings.json 格式异常，已保留原文件")
    try:
        cfg = appconfig.load_config()
        appconfig.apply_settings(cfg, saved)
    except (yaml.YAMLError, TypeError, AttributeError):
        # Parser errors can quote a whole config line containing a secret.
        raise SetupError("现有 config.yaml 格式异常，请检查配置；已保留原文件") from None
    return dict(cfg.get("cloud") or {})


def candidate(data, current, name="", site_type="", terminal=None):
    # 显式参数 > 新码携带的名称/类型 > 现有有效配置 > 新安装默认值。
    chosen_name = name.strip() or str(data.get("n") or "").strip()
    default_name = str(current.get("site_name") or "").strip() or socket.gethostname().split(".")[0] or "site"
    if not chosen_name:
        chosen_name = (terminal.ask("场地名称 [" + default_name + "]，回车保留: ")
                       if terminal else "") or default_name
    chosen_type = site_type or data.get("y")
    if not chosen_type:
        default_type = current.get("site_type") if current.get("site_type") in SITE_TYPES else "air"
        chosen_type = default_type
        if terminal:
            while True:
                answer = terminal.ask("场地类型 1=风冷 2=水冷 3=混合 [" +
                                      str(SITE_TYPES.index(default_type) + 1) + "]，回车保留: ")
                chosen_type = {"1": "air", "2": "hydro", "3": "mixed"}.get(answer, answer) or default_type
                if chosen_type in SITE_TYPES:
                    break
                terminal.write("请输入 1、2、3，或直接回车保留。")
    if chosen_type not in SITE_TYPES:
        raise SetupError("场地类型须为 air、hydro 或 mixed")
    cloud = dict(current)
    cloud.update(enabled=True, url=data["u"], token=data["t"], tls_fingerprint=data["f"],
                 site_name=chosen_name[:64], site_type=chosen_type)
    sid = data.get("s") or current.get("site_id")
    if not sid:
        try:
            with open(cloud_report._ID_FILE, encoding="ascii") as source:
                sid = source.read().strip()
        except FileNotFoundError:
            pass
        except UnicodeError:
            raise SetupError("现有场地 ID 文件内容异常，已保留原配置") from None
    if sid:
        sid = str(sid)
        if site_key(sid) != sid:
            raise SetupError("现有场地 ID 格式异常，请从云端复制本场地的专属接入信息")
    # 候选身份只在测试通过后随 settings 一起保存；失败不生成/改写 cloud_site_id.txt。
    cloud["site_id"] = sid or "st-" + secrets.token_hex(6)
    return cloud


def configure(code, name="", site_type="", terminal=None):
    code = code.strip()
    if code == "-":
        if terminal is None:
            raise SetupError("未收到接入信息，请在终端运行后粘贴，或通过 --stdin 传入。")
        code = ""
    while True:
        if not code:
            if terminal is None:
                raise SetupError("未收到接入信息，请在终端运行后粘贴，或通过 --stdin 传入。")
            code = terminal.ask("粘贴云端接入信息（AMC1-…，输入不显示，回车跳过）: ", secret=True)
            if not code:
                print("已跳过连接云端，原配置未修改。")
                return 10
        data = None
        try:
            data = decode_code(code)
            current = read_state()
            cloud = candidate(data, current, name, site_type, terminal)
            if terminal:
                terminal.write("正在验证云端连接…")
            passed, message = cloud_report.test(cloud, cloud["site_id"])
            if not passed:
                raise SetupError("连接验证未通过：" + message)
            # 测试期间若另一位管理员修改了配置，先重新读取，而不是覆盖新设置。
            latest = read_state()
            if latest != current:
                raise SetupError("云端配置刚被其他操作修改，请重新粘贴接入信息后重试")
            appconfig.update_settings({"cloud": cloud})
            print("连接验证通过，云端配置已保存（场地：" + cloud["site_name"] + "）。")
            return 0
        except (SetupError, OSError, ValueError, RuntimeError) as error:
            message = str(error).replace(code, "[接入信息已隐藏]")
            if isinstance(data, dict) and data.get("t"):
                message = message.replace(data["t"], "[凭据已隐藏]")
            message += "；未修改原云端配置。"
            if terminal is None:
                print(message)
                return 1
            terminal.write(message + "\n请重新粘贴接入信息，或直接回车跳过。")
            code = ""


def start_monitor():
    """Start only when the configured listener is free; never stop another process."""
    if not IS_WINDOWS:
        print("云端配置已保存。请通过现有服务重新启动监控后生效；Ubuntu 可使用 install.sh connect。")
        return 20
    cfg = appconfig.load_config()
    appconfig.apply_settings(cfg, appconfig.load_settings())
    server = cfg.get("server") or {}
    host, port = str(server.get("host") or "0.0.0.0"), int(server.get("port") or 8800)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind((host, port))
    except OSError:
        print("云端配置已保存。监控端口仍在使用或无法确认空闲；请关闭原监控窗口后重新运行 run.bat；以服务方式运行时请重启原服务，使新绑定生效。")
        return 20
    launcher = Path(__file__).resolve().with_name("run.bat")
    try:
        os.startfile(str(launcher))
    except OSError:
        print("云端配置已保存，但未能打开监控窗口。请手动运行原目录的 run.bat。")
        return 20
    print("已打开监控窗口，新绑定将在启动后生效。真实上报结果请查看本地网页的上报状态。")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="粘贴云端给出的整份接入信息，测试后保存本地绑定。")
    parser.add_argument("--stdin", action="store_true", help="从标准输入读入，不把凭据放进命令行参数")
    parser.add_argument("--start", action="store_true", help="Windows：保存后在端口空闲时启动原 run.bat")
    args = parser.parse_args(argv)
    try:
        if args.stdin:
            result = configure(sys.stdin.read(MAX_CODE_LENGTH + 1).strip())
        else:
            print("粘贴云端复制的接入信息即可，无需分别填写地址或 token。")
            result = configure("", terminal=Terminal())
    except (EOFError, KeyboardInterrupt):
        print("已取消连接云端，原配置未修改。")
        return 10
    except SetupError as error:
        print(str(error) + " 原配置未修改。")
        return 1
    except (OSError, ValueError, RuntimeError):
        print("无法读取接入信息或本地配置；原配置未修改，请检查目录权限后重试。")
        return 1
    if result == 0:
        if args.start:
            try:
                return start_monitor()
            except (OSError, ValueError, RuntimeError, yaml.YAMLError, TypeError, AttributeError):
                print("云端配置已保存，但无法确认启动状态。请手动重启原监控并查看上报状态。")
                return 20
        print("配置已保存。请重启现有监控服务使新绑定生效，再查看上报状态。")
    return result


if __name__ == "__main__":
    sys.exit(main())
