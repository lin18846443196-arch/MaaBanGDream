"""离线测试只注册 Python 类，不创建 Agent IPC 或连接模拟器。"""
from maa.agent.agent_server import AgentServer


def _offline_registration(_name):
    return lambda cls: cls


AgentServer.custom_action = staticmethod(_offline_registration)
AgentServer.custom_recognition = staticmethod(_offline_registration)
