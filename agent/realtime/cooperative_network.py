"""协力“断网跳车”的网络控制：按游戏 UID 屏蔽/恢复出口流量。

约束（见 AGENTS 第 22 条）：
- 禁止 `svc wifi` / 飞行模式——它们会顺带打断 MuMu 的 adb 通道，导致
  引擎截图与触控全部中断；
- 必须用 iptables 按游戏 UID 屏蔽出口流量（需要 root），并且 finally
  恢复网络、有界重试，绝不把模拟器留在断网状态。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import re
import shlex


GAME_PACKAGE = "com.bilibili.star.bili"

# adb_shell 的调用协议：输入 shell 参数列表，返回 (returncode, output)。
AdbShell = Callable[[Sequence[str]], tuple[int, str]]


def resolve_game_uid(shell: AdbShell) -> int | None:
    """解析游戏进程的 Linux uid；``dumpsys`` 失败/超时时回退到 /proc。"""
    code, output = shell(("dumpsys", "package", GAME_PACKAGE))
    if code == 0:
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("userId="):
                try:
                    uid = int(stripped.split("=", 1)[1].split()[0])
                    if uid > 0:
                        return uid
                except (ValueError, IndexError):
                    pass
    # 演奏中 dumpsys package 可能超时或输出被截断，导致“无法解析游戏
    # UID”进而门禁 fail-closed。回退到更轻量的 pidof + /proc/<pid>/status，
    # 它不需要包管理器、输出也小得多。
    code, output = shell(("pidof", GAME_PACKAGE))
    if code != 0 or not output.strip():
        # Android 15 reports appId in dumpsys, not a user-specific UID. Ask
        # package manager for the current user when no live process is usable.
        code, output = shell((
            "cmd", "package", "list", "packages", "-U",
            "--user", "current", GAME_PACKAGE,
        ))
        if code == 0:
            for line in output.splitlines():
                match = re.fullmatch(
                    r"package:" + re.escape(GAME_PACKAGE) + r"\s+uid:([1-9][0-9]*)",
                    line.strip(),
                )
                if match:
                    return int(match.group(1))
        return None
    pid = output.strip().split()[0]
    if not re.fullmatch(r'[1-9][0-9]*', pid):
        return None
    code, status = shell(("cat", f"/proc/{pid}/status"))
    if code != 0:
        return None
    for line in status.splitlines():
        if line.startswith("Uid:"):
            try:
                uid = int(line.split()[1])
                return uid if uid > 0 else None
            except (IndexError, ValueError):
                return None
    return None


class GameNetworkGate:
    """按 UID 屏蔽/恢复游戏出口流量；退出时必须恢复网络。"""

    def __init__(self, shell: AdbShell, *, chain_suffix: str = "mbdr_game") -> None:
        self._shell = shell
        self._chain = f"OUTPUT_{chain_suffix}"
        self._blocked = False
        self._dirty = False
        # 设备 shell 是否为 root 的探测结果缓存；None 表示尚未探测。
        self._root_shell_available: bool | None = None
        # 最近一次失败的可读原因，供上层把 fail-closed 的细节交给用户。
        self.last_error: str | None = None
        self.root_access_available: bool | None = None

    @property
    def cleanup_required(self) -> bool:
        return self._dirty

    def _shell_ok(self, args: Sequence[str]) -> bool:
        code, _ = self._shell(args)
        return code == 0

    def _root_shell(self, args: Sequence[str]) -> tuple[int, str]:
        """执行需要 root 的命令；非 root shell 下用 ``su -c`` 提权。

        雷电等模拟器的 adb shell 默认是 uid 2000，iptables 会直接报
        "Permission denied"。这里先探测一次 ``id -u``，不是 root 就
        把整条命令交给 ``su -c``；设备没有 su 或拒绝提权时返回失败，
        保持 fail-closed。探测结果按实例缓存，避免每条命令多一次往返。
        """
        if self._root_shell_available is None:
            code, output = self._shell(("id", "-u"))
            self._root_shell_available = (
                code == 0 and output.strip() == "0"
            )
        if self._root_shell_available:
            return self._shell(args)
        # adb shell 会按空白把参数拆开，su -c 只收一个命令参数；这里把
        # 整条命令用引号包成一个 token，避免 `su -c iptables -L ...`
        # 被拆成多个选项。
        joined = " ".join(str(part) for part in args)
        return self._shell(("su", "-c", f'"{joined}"'))

    def _root_shell_ok(self, args: Sequence[str]) -> bool:
        code, _ = self._root_shell(args)
        return code == 0

    def check_available(self) -> bool:
        """Check firewall access before a room is joined; never change rules."""
        self.last_error = None
        code, output = self._root_shell(("id", "-u"))
        self.root_access_available = code == 0 and output.strip() == "0"
        if not self.root_access_available:
            self.last_error = (
                "断网逃生没有可用的 root 权限；"
                f"root 检查 rc={code}：{output.strip()[:240] or '未返回 root UID'}。"
                "MuMu 已开启 root 时，可先在电脑执行 adb root，再重新连接 Maa"
            )
            return False
        for args, label in (
            (("iptables", "-S", "OUTPUT"), "iptables OUTPUT 访问"),
            (("iptables", "-m", "owner", "-h"), "iptables 游戏 UID 匹配"),
        ):
            code, output = self._root_shell(args)
            if code != 0:
                self.last_error = f"{label}不可用，rc={code}：{output.strip()[:240]}"
                return False
        if resolve_game_uid(self._shell) is None:
            self.last_error = "无法确认当前 Android 用户的游戏 UID，不能定向阻断游戏网络"
            return False
        return True

    def restore_stale_escapes(self) -> bool:
        """Remove verified escape rules left behind by a process crash."""
        self.last_error = None
        code, output = self._root_shell(("iptables", "-S"))
        if code != 0:
            self.last_error = f"无法读取遗留断网规则，rc={code}：{output.strip()[:240]}"
            return False

        def escape_name(name):
            return re.fullmatch(
                r"OUTPUT_(?:mbdr_coop_[0-9a-f]{8}|team_[0-9a-f]{12})", name
            ) is not None

        try:
            snapshot = [shlex.split(line) for line in output.splitlines() if line.strip()]
        except ValueError:
            self.last_error = "遗留断网规则输出无法解析，保留规则并停止恢复"
            return False
        names = set()
        for rule in snapshot:
            if len(rule) >= 2 and rule[0] in ("-N", "-A") and escape_name(rule[1]):
                names.add(rule[1])
            for index, part in enumerate(rule[:-1]):
                if part in ("-j", "--jump", "-g", "--goto") and escape_name(rule[index + 1]):
                    names.add(rule[index + 1])
        if not names:
            return True
        if len(names) > 64:
            self.last_error = "遗留断网链超过恢复上限，停止自动恢复"
            return False
        uid = resolve_game_uid(self._shell)
        if uid is None:
            self.last_error = "无法确认游戏 UID，保留遗留断网规则并停止恢复"
            return False

        # Validate the complete snapshot before any write. A familiar chain
        # name alone never authorizes flushing somebody else's rules.
        plans = {name: {"defined": False, "rules": [], "links": []} for name in names}
        for rule in snapshot:
            if len(rule) >= 2 and rule[1] in names:
                name = rule[1]
                if rule == ["-N", name]:
                    plans[name]["defined"] = True
                elif rule[0] == "-A":
                    expected = ["-A", name, "-m", "owner", "--uid-owner", str(uid), "-j", "REJECT"]
                    if rule not in (
                        expected,
                        expected + ["--reject-with", "icmp-port-unreachable"],
                    ):
                        self.last_error = f"遗留断网链 {name} 含非本游戏逃生规则，保留并停止恢复"
                        return False
                    plans[name]["rules"].append(rule)
                else:
                    self.last_error = f"遗留断网链 {name} 定义异常，保留并停止恢复"
                    return False
            for index, part in enumerate(rule[:-1]):
                if part in ("-j", "--jump", "-g", "--goto") and rule[index + 1] in names:
                    name = rule[index + 1]
                    if rule != ["-A", "OUTPUT", "-j", name]:
                        self.last_error = f"遗留断网链 {name} 含未知引用，保留并停止恢复"
                        return False
                    plans[name]["links"].append(rule)
        if any(not plan["defined"] for plan in plans.values()):
            self.last_error = "遗留断网链定义缺失，保留规则并停止恢复"
            return False
        if any(len(plan["rules"]) > 8 or len(plan["links"]) > 8 for plan in plans.values()):
            self.last_error = "遗留断网链规则超过恢复上限，停止自动恢复"
            return False

        for name, plan in sorted(plans.items()):
            for rule in plan["rules"] + plan["links"]:
                code, output = self._root_shell(("iptables", "-D", *rule[1:]))
                if code != 0:
                    self.last_error = f"无法删除遗留断网规则 {name}，rc={code}：{output.strip()[:240]}"
                    return False
            code, output = self._root_shell(("iptables", "-X", name))
            if code != 0:
                self.last_error = f"无法删除遗留断网链 {name}，rc={code}：{output.strip()[:240]}"
                return False
        code, output = self._root_shell(("iptables", "-S"))
        if code != 0:
            self.last_error = f"遗留断网规则恢复后验证失败，rc={code}：{output.strip()[:240]}"
            return False
        try:
            remaining = [shlex.split(line) for line in output.splitlines() if line.strip()]
        except ValueError:
            self.last_error = "遗留断网规则恢复后输出无法解析"
            return False
        if any(
            (len(rule) >= 2 and rule[0] in ("-N", "-A") and rule[1] in names)
            or any(
                part in ("-j", "--jump", "-g", "--goto") and rule[index + 1] in names
                for index, part in enumerate(rule[:-1])
            )
            for rule in remaining
        ):
            self.last_error = "遗留断网链或引用仍存在，停止继续进入游戏"
            return False
        if self._chain in names:
            self._blocked = self._dirty = False
        return True

    def block(self) -> bool:
        """把游戏 UID 的出口流量 REJECT；重复调用是幂等的。"""
        if self._blocked:
            return True
        self.last_error = None
        diagnostics = []
        def uid_shell(args):
            code, output = self._shell(args)
            # Do not dump package/user data into the task log. Keep bounded
            # errors and distinguish query failure from an unparseable reply.
            detail = (str(output).strip().replace('\n', ' ')[:240] if code else
                      f'查询成功，输出 {len(str(output))} 字符')
            diagnostics.append(f'{args[0]} rc={code}: {detail}')
            return code, output
        uid = resolve_game_uid(uid_shell)
        if uid is None:
            self.last_error = "无法解析游戏 UID；" + '；'.join(diagnostics)
            return False
        # 自建链便于精确恢复，不污染用户既有 OUTPUT 规则。
        if not self._root_shell_ok(
            ("iptables", "-N", self._chain)
        ):
            # 链已存在视为幂等成功。
            code, output = self._root_shell(("iptables", "-L", self._chain))
            if code != 0:
                self.last_error = (
                    f"缺少 root 权限或 iptables 不可用：{output.strip()}"
                )
                return False
        # Mark ownership before either rule write: a failed second command
        # still leaves a jump/chain that must be cleaned in finally.
        self._dirty = True
        created = self._root_shell_ok(
            (
                "iptables", "-I", "OUTPUT", "1",
                "-j", self._chain,
            )
        )
        rejected = self._root_shell_ok(
            (
                "iptables", "-A", self._chain,
                "-m", "owner", "--uid-owner", str(uid),
                "-j", "REJECT",
            )
        )
        self._blocked = created and rejected
        if not self._blocked:
            self.last_error = "iptables 屏蔽规则写入失败"
        return self._blocked

    def restore(self) -> bool:
        """恢复网络：删除规则并清空自建链，容忍部分命令失败。"""
        if not self._dirty:
            return True
        # Do not turn a failed permission/ADB query into evidence of recovery.
        # Keep dirty set after failure, allowing callers' finally retries.
        if not self._root_shell_ok(("iptables", "-F", self._chain)):
            self.last_error = "无法清空游戏断网规则"
            return False
        self._blocked = False  # flushed chain no longer rejects game packets
        for _ in range(8):
            if not self._root_shell_ok(("iptables", "-C", "OUTPUT", "-j", self._chain)):
                break
            if not self._root_shell_ok(("iptables", "-D", "OUTPUT", "-j", self._chain)):
                self.last_error = "无法删除游戏断网链引用"
                return False
        if not self._root_shell_ok(("iptables", "-X", self._chain)):
            self.last_error = "无法删除游戏断网链"
            return False
        self._dirty = False
        return True

    def __enter__(self) -> "GameNetworkGate":
        self.block()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.restore()
