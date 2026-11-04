"""Independent local agent contexts and a barrier across each concurrent decision batch."""
import json
from collections import deque
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import RLock



class Memory:
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


class LocalAgent:
    def __init__(self, name, callback, char_limit=None):
        self.name = name
        self._callback = callback
        self._memory = Memory(char_limit)
        self._lock = RLock()
        self._acting = False

    def observe(self, observation):
        with self._lock:
            if self._acting:
                raise RuntimeError("不能在同一主体判断期间修改其观察")
            self._memory.pre_observe(observation)

    def act(self):
        with self._lock:
            if self._acting:
                raise RuntimeError("同一主体不能同时执行两次判断")
            self._acting = True
            try:
                context = json.loads(self._memory.pre_act(None))
                answer = self._callback(context)
                if not isinstance(answer,str):
                    raise TypeError("主体回调须返回原始文字")
                self._memory.post_act(answer)
                return answer
            finally:
                self._acting = False

    def get_component(self, name, type_=None):
        if name != "memory":
            raise KeyError(name)
        if type_ is not None and not isinstance(self._memory,type_):
            raise TypeError("主体组件类型不符")
        return self._memory

    def get_state(self):
        with self._lock:
            return {"act_component":{}, "context_components":{"memory":self._memory.get_state()}}

    def set_state(self, state):
        with self._lock:
            if self._acting:
                raise RuntimeError("不能在判断期间替换主体记忆")
            self._memory.set_state(state["context_components"]["memory"])


def make_agent(name: str, callback, char_limit=None) -> LocalAgent:
    return LocalAgent(name,callback,char_limit)


def act_together(agents: dict[str,LocalAgent], max_workers: int):
    outputs, errors = {}, {}
    if not agents:
        return outputs, errors
    # Every submitted task finishes before returning a complete batch or a failure.
    with ThreadPoolExecutor(max_workers=max_workers,thread_name_prefix="trantor-agent") as pool:
        pending={key:pool.submit(agent.act) for key,agent in agents.items()}
        for key, future in pending.items():
            try:
                outputs[key]=future.result()
            except Exception as error:
                errors[key]=error
    return outputs, errors


def resolve(world: LocalAgent) -> str:
    return world.act()
