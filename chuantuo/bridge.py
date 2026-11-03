"""Small real Concordia integration, with no inherited global prompt/memory."""
import json
from collections import deque
from copy import deepcopy
from functools import partial

from concordia.agents.entity_agent import EntityAgent
from concordia.typing.entity import ActionSpec, OutputType
from concordia.typing.entity_component import ActingComponent, ContextComponent
from concordia.utils.concurrency import run_tasks_in_background


class Memory(ContextComponent):
    def __init__(self, char_limit=None):
        self.char_limit = char_limit
        self.items = deque()
        self._sizes = deque()
        self._characters = 0
        self.total_items = 0

    def remember(self, kind, text):
        if self.char_limit == 0:
            return
        entry = {"kind":kind,"text":text}
        self.items.append(entry)
        self.total_items += 1
        if self.char_limit is None:
            return
        size = len(json.dumps(entry,ensure_ascii=False)) + 2
        self._sizes.append(size)
        self._characters += size
        # The current observation is also supplied separately. Keep up to one
        # full context of earlier entries so removing its duplicate loses no history.
        capacity = self.char_limit + (size if kind == "observation" else 0)
        while self._characters > capacity and len(self.items) > 1:
            self.items.popleft()
            self._characters -= self._sizes.popleft()

    def pre_observe(self, observation):
        self.remember("observation",observation)
        return ""

    def post_act(self, action_attempt):
        self.remember("own_expression",action_attempt)
        return ""

    def pre_act(self, action_spec):
        items = list(self.items)
        content = items if self.char_limit is None else {"items":items,"total_items":self.total_items}
        return json.dumps(content, ensure_ascii=False)

    def get_state(self):
        result = {"items":deepcopy(list(self.items))}
        if self.char_limit is not None:
            result["total_items"] = self.total_items
        return result

    def set_state(self, state):
        self.items.clear()
        self._sizes.clear()
        self._characters = 0
        self.total_items = 0
        for entry in state["items"]:
            self.remember(entry["kind"],entry["text"])
        self.total_items = state.get("total_items",self.total_items)


class CallModel(ActingComponent):
    def __init__(self, callback):
        self.callback = callback

    def get_action_attempt(self, context, action_spec):
        return self.callback(json.loads(context["memory"]))

    def get_state(self):
        return {}

    def set_state(self, state):
        pass


def make_agent(name: str, callback, char_limit=None) -> EntityAgent:
    return EntityAgent(name, CallModel(callback), context_components={"memory": Memory(char_limit)})


def act_together(agents: dict[str, EntityAgent], max_workers: int):
    spec = ActionSpec(call_to_action="自主表达判断、打算和沟通；不需要内部思维链。", output_type=OutputType.FREE)
    # Unlike the stock Simultaneous engine, caller explicitly rejects an entire
    # window on ANY error. Waiting for all tasks prevents late writes after failure.
    return run_tasks_in_background({key: partial(agent.act, spec) for key, agent in agents.items()}, max_workers=max_workers)


def resolve(world: EntityAgent) -> str:
    return world.act(ActionSpec(call_to_action="推演客观后果并审查全部字段。", output_type=OutputType.RESOLVE))
