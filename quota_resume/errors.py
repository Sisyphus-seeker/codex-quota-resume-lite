"""Closed, actionable messages that cannot expose arbitrary exception text."""

MESSAGES = {
    'old_plugin_active': '旧版 Codex Auto Resume 尚未全局暂停。停止后台进程不等于暂停自动恢复；请先暂停旧版，再运行 Enable.cmd。',
}

DIAGNOSTICS = {
    'desktop_engine_not_identified': '无法确认当前 Codex 桌面引擎，聊天扫描已停止；请检查版本兼容性。',
    'desktop_not_running_or_ambiguous': '未找到唯一的 Codex 桌面进程，聊天扫描已停止。',
    'official_engine_required': 'Codex 引擎路径不受支持。',
    'unsupported_schema': '当前 Codex 数据库结构不兼容。',
    'database_missing': '未找到所需的 Codex 数据库。',
    'queue_interface_changed': '当前 Codex 排队命令接口不兼容。',
    'old_plugin_active': '旧版自动恢复仍处于启用状态。',
}


def diagnostic_reason(exc):
    from .readonly import Unavailable
    return str(exc) if isinstance(exc, Unavailable) and str(exc) in DIAGNOSTICS else None


class ActionError(ValueError):
    def __init__(self, code):
        if code not in MESSAGES:
            raise ValueError('Unknown action error')
        self.code = code
        super().__init__(code)


def describe(exc):
    if isinstance(exc, ActionError):
        return MESSAGES[exc.code]
    code = diagnostic_reason(exc)
    if code:
        return DIAGNOSTICS[code]
    return 'Operation failed: ' + type(exc).__name__ + '; run doctor for compatibility/conflicts'
