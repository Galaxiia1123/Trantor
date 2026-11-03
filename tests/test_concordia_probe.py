"""Release-specific compatibility gate, exercised before product implementation."""
from functools import partial
from importlib.metadata import version

from concordia.agents.entity_agent import EntityAgent
from concordia.typing.entity import ActionSpec, OutputType
from concordia.typing.entity_component import ActingComponent, ContextComponent
from concordia.utils.concurrency import run_tasks_in_background


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
