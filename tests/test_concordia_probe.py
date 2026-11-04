"""Release-specific compatibility gate, exercised before product implementation."""
import json
import pytest
from functools import partial
from importlib.metadata import version

from concordia.agents.entity_agent import EntityAgent
from concordia.typing.entity import ActionSpec, OutputType
from concordia.typing.entity_component import ActingComponent, ContextComponent
from concordia.utils.concurrency import run_tasks_in_background
from chuantuo.bridge import Memory as LocalMemory, make_agent, act_together


class Memory(ContextComponent):
    def __init__(self):
        self.items = []

    def pre_observe(self, observation):
        self.items.append(observation)
        return ""

    def pre_act(self, action_spec):
        return "\n".join(self.items)

    def get_state(self):
        return {"items": list(self.items)}

    def set_state(self, state):
        self.items = list(state["items"])


class Speak(ActingComponent):
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def get_action_attempt(self, context, action_spec):
        self.calls.append(dict(context))
        return self.reply

    def get_state(self):
        return {}

    def set_state(self, state):
        pass


@pytest.mark.parametrize("limit",[None,0,120])
def test_local_context_matches_prior_adapter_for_bounded_and_full_memory(limit):
    class ReferenceMemory(ContextComponent):
        def __init__(self):
            self.memory=LocalMemory(limit)
        def pre_observe(self,value):return self.memory.pre_observe(value)
        def post_act(self,value):return self.memory.post_act(value)
        def pre_act(self,spec):return self.memory.pre_act(spec)
        def get_state(self):return self.memory.get_state()
        def set_state(self,state):self.memory.set_state(state)
    observed=[]
    def reply(context):
        observed.append(context)
        return "本轮表达。"
    native=make_agent("native",reply,limit)
    speech=Speak("本轮表达。")
    legacy=EntityAgent("legacy",speech,context_components={"memory":ReferenceMemory()})
    spec=ActionSpec(call_to_action="Express.",output_type=OutputType.FREE)
    for value in ("private first", "another view"*30, "new situation"):
        native.observe(value);legacy.observe(value)
        answers,errors=act_together({"a":native},1)
        assert not errors and answers["a"]==legacy.act(spec)
        assert observed[-1]==json.loads(speech.calls[-1]["memory"])
    saved=native.get_state()
    saved["context_components"]["memory"]["items"].append({"kind":"observation","text":"outside mutation"})
    assert not any(x["text"]=="outside mutation" for x in native.get_component("memory").items)


def test_real_release_cross_layer_delivery_independent_agents_and_one_resolution():
    assert version("gdm-concordia") == "2.4.0"
    ids = ["country", "department", "employee", "other_company"]
    memory = {key: Memory() for key in ids}
    speech = {key: Speak(f"{key}: I independently consider a joint proposal.") for key in ids}
    agents = {key: EntityAgent(key, speech[key], context_components={"memory": memory[key]}) for key in ids}
    agents["department"].observe("DEPARTMENT_PRIVATE_INFORMATION")
    action_spec = ActionSpec(call_to_action="Decide freely in natural language.", output_type=OutputType.FREE)
    resolutions = []
    for round_index in range(2):
        outputs, errors = run_tasks_in_background({key: partial(agent.act, action_spec) for key, agent in agents.items()})
        assert not errors
        assert set(outputs) == set(ids)
        # The product's independent directed graph can deliver without ancestors
        # relaying the message; all deliveries take place after the batch barrier.
        if round_index == 0:
            agents["country"].observe(outputs["employee"])
            agents["other_company"].observe(outputs["employee"])
            agents["employee"].observe(outputs["country"])
    gm_speech = Speak("A proposed objective consequence, not another actor's decision.")
    gm = EntityAgent("world", gm_speech, context_components={"memory": Memory()})
    gm.observe(str(outputs))
    resolutions.append(gm.act(ActionSpec(call_to_action="Review this whole window.", output_type=OutputType.RESOLVE)))
    assert len(resolutions) == 1
    assert all(len(actor.calls) == 2 for actor in speech.values())
    assert "employee:" in speech["country"].calls[1]["memory"]
    assert "employee:" in speech["other_company"].calls[1]["memory"]
    assert "country:" in speech["employee"].calls[1]["memory"]
    assert "DEPARTMENT_PRIVATE_INFORMATION" not in speech["country"].calls[1]["memory"]
    assert "employee:" not in speech["department"].calls[1]["memory"]
    saved = agents["country"].get_state()
    saved["context_components"]["memory"]["items"].append("external mutation")
    assert "external mutation" not in memory["country"].items
