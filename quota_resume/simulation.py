"""In-memory Codex substitute. It cannot access real chats or send requests."""
from __future__ import annotations

from dataclasses import replace
from uuid import UUID
from .model import Failure, Quota, View


def uid(value: int) -> str:
    return str(UUID(int=value))


class SimSubmission:
    def __init__(self, receipt_id):
        self.receipt_id = receipt_id

    def receipt(self):
        return self.receipt_id


class SimAdapter:
    def __init__(self):
        self.items = []
        self.views = {}
        self.current_quota = Quota(None)
        self.sent = []
        self.uncertain = False

    def failures(self, since):
        return [f for f in self.items if f.at >= since]

    def inspect(self, thread_id):
        return self.views.get(thread_id)

    def quota(self):
        return self.current_quota

    def start(self, thread_id, message, marker):
        self.sent.append({"thread_id": thread_id, "message": message, "marker": marker})
        return SimSubmission(None if self.uncertain else uid(10000 + len(self.sent)))


def demo(store, uncertain=False):
    from .core import Engine
    adapter = SimAdapter()
    adapter.uncertain = uncertain
    for index in (1, 2, 3):
        failure = Failure(uid(index), uid(100 + index), 1010)
        adapter.items.append(failure)
        adapter.views[failure.thread_id] = View(failure.turn_id)
    engine = Engine(store, adapter)
    frames = []

    def frame(now, note):
        engine.tick(now)
        frames.append({"time": now, "event": note,
                       "tasks": [{"task": "任务" + str(int(UUID(r["thread_id"]))),
                                  "state": r["state"], "reason": r["reason"],
                                  "next_check": r["next_check"]} for r in store.rows()],
                       "simulated_sends": len(adapter.sent)})

    adapter.current_quota = Quota(False, 1060, "quota")
    frame(1010, "三个任务因额度停止；按刷新时间等待")
    adapter.current_quota = Quota(False, 1200, "quota")
    adapter.views[uid(3)] = replace(adapter.views[uid(3)], latest_turn=uid(999))
    frame(1090, "周额度仍阻塞；任务3已有用户新回合，取消自动续接")
    adapter.current_quota = Quota(True)
    frame(1230, "额度可用；依次向任务1、2的模拟队列提交")
    frame(1500, "再次轮询相同故障；不重复提交")
    return {"mode": "SIMULATION_ONLY", "live_sending": False,
            "frames": frames, "simulated_sends": len(adapter.sent)}
