"""Measure the first observations and minimum world input without invoking any models."""
from collections import Counter

from .engine import AGENT_SYSTEM, WORLD_SYSTEM, Simulation, encode
from .continuity import actor_instruction, WORLD_INSTRUCTION, Notebook, objective_input
from .models import ModelError, credential_ready
from .perception import Perception
from .schema import RunConfig11, Settlement


def preflight(scene, config, depths):
    catalog = config.catalog
    world_id = config.model_id("world")
    state = scene.initial_state()
    perception = Perception(scene, depths, config)
    opening_events=[{"id":f"e{i:06d}","data":event.model_dump(mode="json")} for i,event in enumerate(perception.calendar.get(1,()),2)]
    calls = Counter({world_id:config.windows})
    blockers = []
    largest = {"actor_id":None,"chars":0}
    for actor in scene.entities:
        key = config.model_id("actor", depths[actor.id])
        calls[key] += config.rounds_per_window * config.windows
        observation = perception.observe(actor.id,state,[],1,1,None,opening_events)
        payload = {"task":"agent","actor_id":actor.id,"window":1,"round":1,"observation":observation,
                   "memory":[{"kind":"observation","text":encode(observation)}]}
        limit = catalog[key].context_limit_chars
        native = isinstance(config, RunConfig11)
        if config.memory_policy == "budgeted":
            try:
                if native:
                    payload = Notebook().payload(observation, config)
                else:
                    Simulation._fit_private_memory(payload,limit)
            except ModelError as error:
                blockers.append(f"{actor.name}（{key}）：{error}")
                continue
        size = len(actor_instruction(config) if native else AGENT_SYSTEM) + len(encode(payload))
        if size > largest["chars"]:
            largest = {"actor_id":actor.id,"chars":size}
        if size > limit:
            blockers.append(f"{actor.name}（{key}）首轮需 {size} 字符，上限为 {limit}")
    world = {"task":"world","window":1,"window_duration":config.window_duration,
             "scenario":scene.world_view(config.world_source_policy == "defined"),"state":state,"actions":[],
             "history":[],"response_schema":Settlement.model_json_schema()}
    world_floor = len(WORLD_SYSTEM) + len(encode(world))
    if isinstance(config, RunConfig11):
        world = objective_input(scene,config,state,[],[],1,Settlement.model_json_schema(),opening_events)
        world_floor = len(WORLD_INSTRUCTION) + len(encode(world))
    world_limit = catalog[world_id].context_limit_chars
    if world_floor > world_limit:
        blockers.append(f"世界模型尚未计入任何行动便需 {world_floor} 字符，上限为 {world_limit}。可选择只发送已审阅定义与原件索引")
    return {"agent_calls":len(depths)*config.rounds_per_window*config.windows,"world_calls":config.windows,
            "baseline_calls":sum(calls.values()),"by_model":dict(calls),"max_parallel":min(len(depths),config.max_parallel),
            "missing_credentials":[key for key in sorted(calls) if not credential_ready(catalog[key])],
            "first_round_largest":largest,"world_minimum_chars":world_floor,"blocking_context":blockers}
