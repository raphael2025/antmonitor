# -*- coding: utf-8 -*-
"""网页/命令行改密码会原地改写 config.yaml：必须只动那一个字段，改不对就一个字节都不写。"""
import yaml

import appconfig

FLOW = '''auth:
  enabled: true   # 注释要保留
  # 改密码: 运行 python auth.py passwd
  users:
    - {username: admin,  password: "改成 python auth.py 生成的哈希", role: admin}
    - {username: ops,    password: "改成 python auth.py 生成的哈希", role: ops}
control:
  enabled: true
'''

BLOCK = '''auth:
  users:
    - username: admin
      password: admin888   # 旧密码
      role: admin
    - role: ops
      password: 'x'
      username: "ops"
scan:
  passwords:
    - ["root", "root"]
'''


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_flow_style_only_target_user_changes_and_comments_survive(tmp_path):
    p = _write(tmp_path, FLOW)
    assert appconfig.set_user_password("ops", "pbkdf2$1$ab$cd", str(p)) == ""
    text = p.read_text(encoding="utf-8")
    users = {u["username"]: u for u in yaml.safe_load(text)["auth"]["users"]}
    assert users["ops"]["password"] == "pbkdf2$1$ab$cd" and users["ops"]["role"] == "ops"
    assert users["admin"]["password"].startswith("改成")
    assert "# 注释要保留" in text and "# 改密码" in text
    assert (tmp_path / "config.yaml.bak").read_text(encoding="utf-8") == FLOW


def test_block_style_including_password_before_username(tmp_path):
    p = _write(tmp_path, BLOCK)
    assert appconfig.set_user_password("admin", "pbkdf2$1$aa$bb", str(p)) == ""
    assert appconfig.set_user_password("ops", "pbkdf2$1$cc$dd", str(p)) == ""
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    users = {u["username"]: u for u in d["auth"]["users"]}
    assert users["admin"] == {"username": "admin", "password": "pbkdf2$1$aa$bb", "role": "admin"}
    assert users["ops"] == {"username": "ops", "password": "pbkdf2$1$cc$dd", "role": "ops"}
    assert d["scan"]["passwords"] == [["root", "root"]]


def test_unknown_user_or_missing_password_line_writes_nothing(tmp_path):
    p = _write(tmp_path, BLOCK.replace("      password: admin888   # 旧密码\n", ""))
    before = p.read_text(encoding="utf-8")
    assert appconfig.set_user_password("admin", "pbkdf2$1$aa$bb", str(p))
    assert appconfig.set_user_password("nobody", "pbkdf2$1$aa$bb", str(p))
    assert p.read_text(encoding="utf-8") == before
