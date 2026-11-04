"""Evaluate one shared capacity against a simultaneous, user-defined field snapshot."""
from decimal import Decimal, localcontext


def _decimal(value):
    return Decimal(str(value))


def shown(value):
    if value is None:
        return None
    plain = format(value,"f")
    return plain.rstrip("0").rstrip(".") if "." in plain else plain


def capacity_use(pool, state):
    capacity = pool.limit
    if hasattr(capacity,"entity_id"):
        capacity = state[capacity.entity_id][capacity.field_id]
    entries = [state[slot.entity_id][slot.field_id] for slot in pool.members]
    known = [v for v in entries if v is not None]
    if any(v < 0 for v in known) or capacity is not None and capacity < 0:
        raise ValueError(f"共享上限“{pool.name}”的容量和各项占用必须非负，请核对本窗字段值")
    numbers = [_decimal(v) for v in known]
    scale = numbers + ([_decimal(capacity)] if capacity is not None else []) + [Decimal(0)]
    unknown = len(entries)-len(known)
    with localcontext() as ctx:
        ctx.prec = max(28,max(v.adjusted() for v in scale)-min(v.as_tuple().exponent for v in scale)+len(str(len(scale)))+3)
        used = sum(numbers,Decimal(0))
        if capacity is not None and used > _decimal(capacity):
            raise ValueError(f"共享上限“{pool.name}”为 {capacity}，已知占用合计 {shown(used)}，超过 {shown(used-_decimal(capacity))}。"
                             "同一窗口需同时满足上限及各项行动依据；不能把未经确认的申请自动记为分配")
        free = _decimal(capacity)-used if capacity is not None and not unknown else None
    return {"capacity":capacity,"known_used":shown(used),"unknown":unknown,"remaining":shown(free),
            "status":"unknown" if free is None else "full" if free==0 else "available"}


def validate_pools(scene):
    definitions = {(a.id,f.id):f for a in scene.entities for f in a.fields}
    names = set()
    initial = scene.initial_state()
    for pool in scene.capacity_pools:
        if pool.id in names:
            raise ValueError("共享上限编号重复")
        names.add(pool.id)
        if not set(pool.visibility.readers)<={a.id for a in scene.entities}:
            raise ValueError(f"共享上限“{pool.name}”含有未定义的知情主体")
        if pool.provenance.kind not in {"user","material"} or pool.provenance.kind=="material" and not pool.provenance.quotes:
            raise ValueError(f"共享上限“{pool.name}”需由用户明确设定或引用陈述约束的原文")
        members = [(ref.entity_id,ref.field_id) for ref in pool.members]
        keys = list(members)
        if hasattr(pool.limit,"entity_id"):
            keys.append((pool.limit.entity_id,pool.limit.field_id))
        if len(keys)!=len(set(keys)):
            raise ValueError(f"共享上限“{pool.name}”不能重复计数或把容量自身列为占用")
        if any(key not in definitions or definitions[key].kind!="number" for key in keys):
            raise ValueError(f"共享上限“{pool.name}”只能引用场景中已定义的数值字段")
        if len({definitions[key].unit or "" for key in keys})>1:
            raise ValueError(f"共享上限“{pool.name}”的容量与各项占用必须使用相同单位")
        capacity_use(pool,initial)


def visible_capacity(pool, visible, state):
    refs = list(pool.members)
    by_field = hasattr(pool.limit,"entity_id")
    if by_field:
        refs.append(pool.limit)
    all_visible = all(ref.field_id in visible.get(ref.entity_id,{}) for ref in refs)
    if all_visible:
        usage=capacity_use(pool,state)
        unit=visible[pool.members[0].entity_id][pool.members[0].field_id].get("unit") or ""
        if usage["remaining"] is not None:
            return f"{pool.name}：现有余量 {usage['remaining']}{unit}。"
        return f"{pool.name}：容量或部分占用未知，余量尚无法确定。"
    # The rule can be public while another participant's actual allocation stays private.
    return f"{pool.name}：此项占用受共同容量限制。"


def capacities_at(sim, window):
    from .experiments import boundary
    snap=boundary(sim,window)
    actors={a.id:a for a in sim._scenario.entities}
    fields={(a.id,f.id):f for a in actors.values() for f in a.fields}
    def entry(ref):
        field=fields[ref.entity_id,ref.field_id]
        return {"actor":ref.entity_id,"name":actors[ref.entity_id].name,"field":ref.field_id,"label":field.label or field.id,
                "value":snap["state"][ref.entity_id][ref.field_id]}
    pools=[]
    for pool in getattr(sim._scenario,"capacity_pools",()):
        first=pool.members[0]
        pools.append({"id":pool.id,"name":pool.name,"unit":fields[first.entity_id,first.field_id].unit,
                      "usage":capacity_use(pool,snap["state"]),"members":[entry(ref) for ref in pool.members],
                      "capacity_field":entry(pool.limit) if hasattr(pool.limit,"entity_id") else None,
                      "provenance":pool.provenance.model_dump(mode="json",exclude_defaults=True)})
    return {"window":snap["window"],"record":snap["event_id"],"pools":pools}
