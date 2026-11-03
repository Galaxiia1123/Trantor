"""The prototype's explicit configuration format; no built-in domain fields."""
from __future__ import annotations

import math
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from .models import ModelConfig

Identifier = Annotated[str, Field(min_length=1, pattern=r"^[\w.-]+$")]
Value = StrictBool | StrictInt | StrictFloat | StrictStr | None


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceQuote(Record):
    source_id: Identifier
    quote: str = Field(min_length=1, max_length=1200)
    page: int | None = Field(default=None, ge=1)


class Provenance(Record):
    kind: Literal["user", "material", "ai_assumption", "missing"] = "user"
    source_ids: list[str] = Field(default_factory=list)
    explanation: str = "用户显式配置"
    quotes: list[SourceQuote] = Field(default_factory=list)


class SourcePage(Record):
    number: int = Field(ge=1)
    method: Literal["text", "pdf_text", "ocr"]
    text_start: int = Field(ge=0)
    text_end: int = Field(ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)
    preview_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class SimulationReference(Record):
    run_id: str
    window: int = Field(ge=0)
    event_id: str = Field(pattern=r"^e[0-9]{6,}$")
    state_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PublicReference(Record):
    url: str = Field(pattern=r"^https://", max_length=2048)
    query: str = Field(min_length=1, max_length=160)
    retrieved_at: str
    published: str | None = None
    service: str


class Source(Record):
    id: Identifier
    name: str
    text: str
    asset_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    mime_type: str | None = None
    sha256: str | None = None
    simulation_ref: SimulationReference | None = None
    public_reference: PublicReference | None = None
    pages: list[SourcePage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def origin_context(self):
        if self.public_reference:
            return {"public_reference": self.public_reference.model_dump(exclude_defaults=True)}
        if self.simulation_ref:
            return {"simulation_ref": self.simulation_ref.model_dump()}
        return {}

    @model_validator(mode="after")
    def valid_page_spans(self):
        last_number, last_end = 0, 0
        for page in self.pages:
            if page.number <= last_number or not last_end <= page.text_start <= page.text_end <= len(self.text):
                raise ValueError("材料页码或正文范围无效")
            last_number, last_end = page.number, page.text_end
        return self


class ReaderGrant(Record):
    reader_id: Identifier
    quote: SourceQuote


class Visibility(Record):
    public: bool = False
    readers: list[str] = Field(default_factory=list)
    reader_evidence: list[ReaderGrant] = Field(default_factory=list)

    def allows(self, owner: str, viewer: str) -> bool:
        return self.public or owner == viewer or viewer in self.readers


def valid_value(kind: str, value: Value) -> bool:
    if value is None:
        return True
    if kind == "number":
        return type(value) is int or (type(value) is float and math.isfinite(value))
    return (kind == "string" and type(value) is str) or (kind == "boolean" and type(value) is bool)


class EntryWindow(Record):
    value: StrictStr
    first: int = Field(default=1,ge=1,strict=True)
    last: int | None = Field(default=None,ge=1,strict=True)
    closed_before_start: bool = False
    provenance: Provenance = Field(default_factory=Provenance)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.last is not None and self.last<self.first:
            raise ValueError("允许转入该值的最后一窗须不早于第一窗")
        if self.provenance.kind not in {"material","user"} or (self.provenance.kind=="material" and not self.provenance.quotes):
            raise ValueError("字段值的转入期限需要用户设定或可核对的原文")
        return self

    def allows(self, window):
        return not self.closed_before_start and self.first<=window and (self.last is None or window<=self.last)


class ValueMeaning(Record):
    value: StrictStr
    text: str = Field(min_length=1,max_length=500)
    provenance: Provenance = Field(default_factory=Provenance)

    @model_validator(mode="after")
    def grounded_definition(self):
        if self.provenance.kind not in {"user","material"} or self.provenance.kind=="material" and not self.provenance.quotes:
            raise ValueError("候选值的含义需要用户设定或明确的材料原句")
        if not self.text.strip():
            raise ValueError("请说明这个值在本场景中代表什么")
        return self


class FieldConstraints(Record):

    minimum: StrictInt | StrictFloat | None = None
    maximum: StrictInt | StrictFloat | None = None
    choices: list[StrictStr] = Field(default_factory=list)
    allow_unknown: bool = True
    fixed: bool = False
    entry_windows: list[EntryWindow] = Field(default_factory=list)
    meanings: list[ValueMeaning] = Field(default_factory=list,max_length=64)


class StateField(Record):
    id: Identifier
    label: str | None = Field(default=None, min_length=1)
    domain: str
    kind: Literal["number", "string", "boolean"]
    value: Value
    unit: str | None = None
    visibility: Visibility = Field(default_factory=Visibility)
    provenance: Provenance = Field(default_factory=Provenance)
    constraints: FieldConstraints | None = None

    def entry_period(self,value):
        return next((r for r in self.constraints.entry_windows if r.value==value),None) if self.constraints else None

    def permits(self, value):
        if not valid_value(self.kind,value):
            return False
        rule = self.constraints
        if rule is None:
            return True
        if value is None:
            return rule.allow_unknown
        if self.kind == "number":
            return (rule.minimum is None or value >= rule.minimum) and (rule.maximum is None or value <= rule.maximum)
        return not rule.choices or value in rule.choices

    @model_validator(mode="after")
    def check_value(self):
        if not valid_value(self.kind, self.value):
            raise ValueError(f"字段 {self.id} 与类型 {self.kind} 不匹配")
        rule = self.constraints
        if rule:
            bounds = [bound for bound in (rule.minimum,rule.maximum) if bound is not None]
            if bounds and (self.kind != "number" or not all(valid_value("number",v) for v in bounds)):
                raise ValueError("只有数值字段可设置有限的上下界")
            if len(bounds) == 2 and rule.minimum > rule.maximum:
                raise ValueError("字段下界大于上界")
            if rule.choices and (self.kind != "string" or len(set(rule.choices)) != len(rule.choices)):
                raise ValueError("候选值仅用于文字字段，且不能重复")
            bounds=[r.value for r in rule.entry_windows]
            if len(bounds)!=len(set(bounds)) or not set(bounds)<=set(rule.choices):
                raise ValueError("转入期限只能分别设在本字段已经定义的文字候选值上")
            meanings=[m.value for m in rule.meanings]
            if len(meanings)!=len(set(meanings)) or not set(meanings)<=set(rule.choices):
                raise ValueError("每个已有文字候选值只能设定一种含义")
            if not self.permits(self.value):
                raise ValueError(f"字段 {self.id} 的初始值不符合所设约束")
        return self


class SharingRule(Record):
    allowed_recipients: list[Identifier] | None = None
    excluded_recipients: list[Identifier] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


class DocumentLabel(Record):
    id: Identifier
    title: str = Field(min_length=1,max_length=100,pattern=r"\S")


class Material(Record):
    domain: str
    text: str
    visibility: Visibility = Field(default_factory=Visibility)
    provenance: Provenance = Field(default_factory=Provenance)
    protected_terms: list[str] = Field(default_factory=list, max_length=32)
    sharing: SharingRule | None = None
    document: DocumentLabel | None = None

    @model_validator(mode="after")
    def literal_private_terms(self):
        if any(not term.strip() or term not in self.text for term in self.protected_terms):
            raise ValueError("保密短语须逐字出现在该资料中")
        if self.protected_terms and self.visibility.public:
            raise ValueError("含保密短语的资料需明确知情范围")
        if self.sharing is not None and not self.protected_terms and self.document is None:
            raise ValueError("通信保密范围需要可核对的资料原文短语")
        if self.document:
            if not self.text.strip() or len(self.text)>1200:
                raise ValueError("可寄送的原件正文须为 1 至 1200 字，长文可选取一段完整的凭证原文")
            if self.provenance.kind not in {"material","user"} or (self.provenance.kind=="material" and
                    not any(self.text in q.quote for q in self.provenance.quotes)):
                raise ValueError("原件正文需要操作者明确提供，或逐字包含在所引用的材料中")
        return self


class Actor(Record):
    id: Identifier
    name: str
    identity: str
    preferences: str = ""
    subsystem_id: str | None = None
    parent_id: Identifier | None = None
    fields: list[StateField] = Field(default_factory=list)
    materials: list[Material] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


class Subsystem(Record):
    id: Identifier
    name: str
    owner_id: str
    interaction: str = ""


class Influence(Record):
    source: str
    target: str
    weight: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    provenance: Provenance = Field(default_factory=Provenance)


class Relation(Record):
    source: Identifier
    target: Identifier
    description: str = Field(min_length=1, max_length=1600, pattern=r"\S")
    provenance: Provenance = Field(default_factory=Provenance)


class Rule(Record):
    text: str
    provenance: Provenance = Field(default_factory=Provenance)


class ScopedRule(Rule):
    # Without an explicit audience a world rule belongs only to the adjudicator.
    visibility: Visibility = Field(default_factory=Visibility)
    active_from: int = Field(default=1, ge=1)
    active_until: int | None = Field(default=None,ge=1,strict=True)

    @model_validator(mode="after")
    def check_period(self):
        if self.active_until is not None and self.active_until<self.active_from:
            raise ValueError("规则的最后有效窗口不能早于生效窗口")
        return self

    def applies(self,window):
        return self.active_from<=window and (self.active_until is None or window<=self.active_until)


class ScheduledValue(Record):
    entity_id: Identifier
    field_id: Identifier
    value: Value


class ScheduledEvent(Record):
    id: Identifier
    window: int = Field(ge=1,strict=True)
    text: str = Field(min_length=1,max_length=1600,pattern=r"\S")
    visibility: Visibility = Field(default_factory=Visibility)
    assignments: list[ScheduledValue] = Field(default_factory=list,max_length=64)
    provenance: Provenance = Field(default_factory=Provenance)


class Scenario(Record):
    title: str
    background: str
    domains: list[str]
    entities: list[Actor] = Field(min_length=1)
    subsystems: list[Subsystem] = Field(default_factory=list)
    influences: list[Influence] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_structure(self):
        actors = {a.id: a for a in self.entities}
        systems = {s.id: s for s in self.subsystems}
        source_ids = {s.id for s in self.sources}
        sources = {s.id: s for s in self.sources}
        if len(actors) != len(self.entities) or len(systems) != len(self.subsystems):
            raise ValueError("主体和子系统 ID 必须分别唯一")
        if len(source_ids) != len(self.sources) or len(set(self.domains)) != len(self.domains):
            raise ValueError("材料 ID 和领域名称必须分别唯一")
        documents=[m.document.id for a in self.entities for m in a.materials if m.document]
        if documents and (getattr(self,"format_version",None)!="1.1" or len(set(documents))!=len(documents)):
            raise ValueError("可寄送的原件需使用单模型场景，每份原件在整个场景中有独立编号")
        for system in self.subsystems:
            if system.owner_id not in actors:
                raise ValueError(f"子系统 {system.id} 的 owner_id 不存在")
        for actor in self.entities:
            if actor.subsystem_id is not None and actor.subsystem_id not in systems:
                raise ValueError(f"主体 {actor.id} 的 subsystem_id 不存在")
            if actor.parent_id is not None and actor.parent_id not in actors:
                raise ValueError(f"主体 {actor.id} 的上级不存在")
            if actor.parent_id and actor.subsystem_id and systems[actor.subsystem_id].owner_id != actor.parent_id:
                raise ValueError(f"主体 {actor.id} 的直接归属与子系统所属主体冲突")
            if len({f.id for f in actor.fields}) != len(actor.fields):
                raise ValueError(f"主体 {actor.id} 的字段 ID 重复")
            for item in [*actor.fields, *actor.materials]:
                if item.domain not in self.domains:
                    raise ValueError(f"未定义领域: {item.domain}")
                if not set(item.visibility.readers) <= actors.keys():
                    raise ValueError("可见性白名单包含不存在的主体")
        for edge in self.influences:
            if edge.source not in actors or edge.target not in actors:
                raise ValueError("影响边的源或目标不存在")
            if isinstance(edge, Influence) and edge.weight is not None and edge.provenance.kind == "ai_assumption" and not edge.provenance.explanation.strip():
                raise ValueError("AI 估计的权重必须包含理由")
        if len({(e.source, e.target) for e in self.influences}) != len(self.influences):
            raise ValueError("同一有向边重复；反向边可以独立配置")
        for obj in [*self.entities, *self.influences, *self.rules, *getattr(self,"scheduled_events",[]), *getattr(self,"mail_routes",[]), *[f for a in self.entities for f in [*a.fields, *a.materials]],
                    *(m.sharing for a in self.entities for m in a.materials if m.sharing),
                    *(period for a in self.entities for f in a.fields if f.constraints for period in f.constraints.entry_windows),
                    *(m for a in self.entities for f in a.fields if f.constraints for m in f.constraints.meanings),
                    *([self.public_brief] if getattr(self,"public_brief",None) is not None else [])]:
            if not set(obj.provenance.source_ids) <= source_ids:
                raise ValueError("来源引用不存在")
            for quote in obj.provenance.quotes:
                if quote.source_id not in obj.provenance.source_ids:
                    raise ValueError("原文摘引未关联到该定义的材料")
                source = sources[quote.source_id]
                text = source.text
                if quote.page is not None:
                    page = next((p for p in source.pages if p.number == quote.page), None)
                    if page is None:
                        raise ValueError("摘引的材料页码不存在")
                    text = text[page.text_start:page.text_end]
                if quote.quote not in text:
                    raise ValueError("材料摘引与指定原文不符，请逐字核对")
        self.depths()  # Detect containment cycles independently of influence cycles.
        return self

    def depths(self) -> dict[str, int]:
        actors = {a.id: a for a in self.entities}
        systems = {s.id: s for s in self.subsystems}
        depths: dict[str, int] = {}
        for actor_id in actors:
            path = []
            seen = set()
            current = actor_id
            while current not in depths:
                if current in seen:
                    raise ValueError("包含结构不能循环；影响图允许反馈环")
                path.append(current)
                seen.add(current)
                actor = actors[current]
                parent = actor.parent_id or (systems[actor.subsystem_id].owner_id if actor.subsystem_id else None)
                if parent is None:
                    depth = -1
                    break
                current = parent
            else:
                depth = depths[current]
            for node in reversed(path):
                depth += 1
                depths[node] = depth
        return depths

    def initial_state(self) -> dict[str, dict[str, Value]]:
        return {a.id: {f.id: f.value for f in a.fields} for a in self.entities}

    def world_view(self, defined_only=False):
        if not defined_only:
            return self.model_dump(mode="json")
        result = self.model_dump(mode="json", exclude={"sources"})
        result["sources"] = [{"id":source.id, "name":source.name, "sha256":source.sha256, "characters":len(source.text),
                              "simulation_ref":source.simulation_ref.model_dump() if source.simulation_ref else None} for source in self.sources]
        result["source_scope"] = "配置采用已审阅的定义、领域资料及摘引。sources 为原件索引，未向本次调用提供原文全文。"
        return result


class RouteTiming(Record):
    source: Identifier
    target: Identifier
    transit_windows: int = Field(ge=0,le=365,strict=True)


class MailRoute(RouteTiming):
    active_from: int = Field(default=1,ge=1)
    provenance: Provenance = Field(default_factory=Provenance)


class PublicBrief(Record):
    text: str = Field(max_length=4800)
    provenance: Provenance = Field(default_factory=Provenance)


class Scenario11(Scenario):
    format_version: Literal["1.1"] = "1.1"
    influences: list[Relation] = Field(default_factory=list)
    rules: list[ScopedRule] = Field(default_factory=list)
    scheduled_events: list[ScheduledEvent] = Field(default_factory=list,max_length=256)
    mail_routes: list[MailRoute] = Field(default_factory=list,max_length=256)
    public_brief: PublicBrief | None = None

    @model_validator(mode="after")
    def validate_calendar(self):
        actors={a.id for a in self.entities}
        slots=set()
        for route in self.mail_routes:
            key=(route.source,route.target,route.active_from)
            if route.source==route.target or not {route.source,route.target}<=actors or key in slots:
                raise ValueError("通信时效需要两个不同的已定义主体，同一方向和生效窗口只能设定一次")
            if route.provenance.kind not in {"user","material"} or (route.provenance.kind=="material" and not route.provenance.quotes):
                raise ValueError("通信时效需要明确设定或带原文出处的材料")
            slots.add(key)
        fields={(a.id,f.id):f for a in self.entities for f in a.fields}
        seen=set()
        if len({s.id for s in self.scheduled_events})!=len(self.scheduled_events):
            raise ValueError("每个预定外部事件需要独立编号")
        for event in self.scheduled_events:
            if not set(event.visibility.readers)<=actors:
                raise ValueError("外部事件的知情范围包含不存在的主体")
            if event.assignments and event.provenance.kind not in {"user","material"}:
                raise ValueError("预定字段值必须来自用户设定或可核对的材料")
            if event.assignments and event.provenance.kind=="material" and not event.provenance.quotes:
                raise ValueError("文稿预定的字段变更需引用原文")
            for effect in event.assignments:
                field=fields.get((effect.entity_id,effect.field_id))
                if not field or not field.permits(effect.value) or (field.constraints and field.constraints.fixed and effect.value!=field.value):
                    raise ValueError(f"外部事件 {event.id} 的字段不存在、越界或与固定值冲突")
                period=field.entry_period(effect.value)
                if period and not period.allows(event.window):
                    raise ValueError(f"外部事件 {event.id} 强制赋值的时点不在该字段值的转入期限内")
                key=(event.window,effect.entity_id,effect.field_id)
                if key in seen:
                    raise ValueError("同一窗口的同一字段不能接受两次预定赋值")
                seen.add(key)
        return self

    def public_amounts(self):
        return {v for a in self.entities for f in a.fields if f.kind=="number" and f.value is not None and f.visibility.public
                for v in (str(f.value)+(f.unit or ""),str(f.value).removesuffix(".0")+(f.unit or ""))}

    def delivery_limits(self):
        ids = {a.id for a in self.entities}
        amounts = self.public_amounts()
        limits = {}
        for actor in self.entities:
            for material in actor.materials:
                rule = material.sharing
                if rule is None:
                    continue
                excluded = set(rule.excluded_recipients)
                allowed = ids.copy() if rule.allowed_recipients is None else {actor.id,*rule.allowed_recipients}
                if not (allowed | excluded) <= ids or actor.id in excluded:
                    raise ValueError("通信保密范围的主体或所属人无效")
                allowed -= excluded
                if not set(material.visibility.readers) <= allowed:
                    raise ValueError("开局知情范围与保密资料通信权限冲突")
                for term in material.protected_terms:
                    if term not in amounts:
                        limits.setdefault(term,ids.copy()).intersection_update(allowed)
        return limits

    @model_validator(mode="after")
    def simplify_containers(self):
        from collections import Counter
        members = Counter(actor.subsystem_id for actor in self.entities)
        owners = {s.id: s.owner_id for s in self.subsystems if members[s.id] < 2 or not s.interaction.strip()}
        if owners:
            object.__setattr__(self, "entities", [
                a.model_copy(update={"parent_id": owners[a.subsystem_id], "subsystem_id": None})
                if a.subsystem_id in owners else a for a in self.entities
            ])
            object.__setattr__(self, "subsystems", [s for s in self.subsystems if s.id not in owners])
        return self

    @model_validator(mode="after")
    def check_private_literals(self):
        actor_ids = {a.id for a in self.entities}
        # Knowing a public budget of 2000万元 does not reveal that a different, private
        # guarantee has the same amount. Protect the private association, not coincident numerals.
        public_amounts = self.public_amounts()
        self.delivery_limits()
        private = {}
        for event in self.scheduled_events:
            if not event.visibility.public and len(event.text.strip())>=8:
                private.setdefault(event.text.strip(),set()).update(event.visibility.readers)
        for actor in self.entities:
            for field in actor.fields:
                if not field.visibility.public and isinstance(field.value,str):
                    value = field.value.strip()
                    distinctive = len(value) >= 12 or (len(value) >= 4 and re.search(r"\d",value) and re.search(r"[^\W\d_]",value))
                    if distinctive and value not in public_amounts:
                        private.setdefault(value,set()).update([actor.id,*field.visibility.readers])
            for material in actor.materials:
                if not material.visibility.public:
                    # Short categorical answers can coincide across unrelated fields. Explicit
                    # protected terms still apply even when the phrase is short.
                    terms = list(material.protected_terms)
                    if len(material.text.strip()) >= 8:
                        terms.append(material.text.strip())
                    for term in terms:
                        if term and term not in public_amounts:
                            private.setdefault(term,set()).update([actor.id,*material.visibility.readers])

        def check(text, readers, location):
            if any(term in text and not readers <= allowed for term,allowed in private.items()):
                raise ValueError(f"{location}含有超出读者权限的私有内容。请调整可见范围，或把涉密内容移入主体资料")
        check(self.public_brief.text if self.public_brief is not None else self.background, actor_ids, "公共背景")
        from .briefing import check_public_numbers
        check_public_numbers(self)
        for actor in self.entities:
            for f in actor.fields:
                for meaning in f.constraints.meanings if f.constraints else ():
                    check(meaning.text,actor_ids if f.visibility.public else {actor.id,*f.visibility.readers},"字段值的含义")
        for system in self.subsystems:
            check(system.name, actor_ids, "子系统名称")
            check(system.interaction, {system.owner_id, *(a.id for a in self.entities if a.subsystem_id == system.id)}, "子系统互动说明")
        for index, rule in enumerate(self.rules,1):
            if not set(rule.visibility.readers) <= actor_ids:
                raise ValueError("规则的知情名单包含不存在的主体")
            check(rule.text, actor_ids if rule.visibility.public else set(rule.visibility.readers), f"第{index}条规则")
        fields={(a.id,f.id):f for a in self.entities for f in a.fields}
        for event in self.scheduled_events:
            check(event.text,actor_ids if event.visibility.public else set(event.visibility.readers),"外部事件通知")
            for effect in event.assignments:
                field=fields.get((effect.entity_id,effect.field_id))
                if field and isinstance(effect.value,str):
                    check(effect.value,actor_ids if field.visibility.public else {effect.entity_id,*field.visibility.readers},"外部事件的字段变更")
        for edge in self.influences:
            check(edge.description, {edge.source,edge.target}, "有向关系描述")
        for actor in self.entities:
            check(actor.name,actor_ids,"主体名称")
            check(actor.identity + "\n" + actor.preferences,{actor.id},"主体档案")
            for obj in [*actor.materials,*actor.fields]:
                value = obj.text if isinstance(obj,Material) else obj.value
                readers = actor_ids if obj.visibility.public else {actor.id,*obj.visibility.readers}
                if isinstance(value,str):
                    check(value, readers, "主体字段或资料")
                if isinstance(obj,StateField):
                    check((obj.label or "") + (obj.unit or ""), readers, "字段说明")
        return self


def load_scenario(data):
    return (Scenario11 if data.get("format_version") == "1.1" else Scenario).model_validate(data)


def message_scope(text, limits):
    return {term:sorted(readers) for term,readers in limits.items() if term in text}


def validate_outbox(expression, limits):
    for index,message in enumerate(expression.messages,1):
        for term,allowed in message_scope(message.text,limits).items():
            denied = sorted(set(message.recipients) - set(allowed))
            if denied:
                raise ValueError(f"第{index}条消息含受限短语“{term}”，不能投递给 {'、'.join(denied)}。请仅发给有权的接收方，或另写可公开的进展")


class ModelAssignments(Record):
    configs: dict[str, ModelConfig]
    world: str
    layers: dict[int, str]
    parser: str
    report: str  # Explicit choice; no unconfirmed inheritance rule.

    @model_validator(mode="after")
    def references(self):
        for key in [self.world, self.parser, self.report, *self.layers.values()]:
            if key not in self.configs:
                raise ValueError(f"模型配置不存在: {key}")
        return self


class RunSettings(Record):
    window_duration: str = Field(min_length=1)
    windows: int = Field(gt=0)
    rounds_per_window: int = Field(gt=0)
    max_parallel: int = Field(default=4, gt=0, le=64)
    memory_policy: Literal["full", "budgeted"] = "full"
    roster_policy: Literal["all", "neighbors"] = "all"
    world_source_policy: Literal["full", "defined"] = "full"
    settlement_retries: int = Field(default=0, ge=0, le=2)
    model_call_limit: int | None = Field(default=None, gt=0)

class RunConfig(RunSettings):
    models: ModelAssignments

    @property
    def catalog(self):
        return self.models.configs

    def model_id(self, role, depth=None):
        return self.models.layers[depth] if role == "actor" else getattr(self.models, role)


class RunPlan(RunSettings):
    window_duration: str = Field(default="1 个模拟日", min_length=1)
    windows: int = Field(default=5, gt=0)
    rounds_per_window: int = Field(default=2, gt=0)
    memory_policy: Literal["budgeted"] = "budgeted"
    roster_policy: Literal["all", "neighbors"] = "neighbors"
    world_source_policy: Literal["defined"] = "defined"
    recent_records: int = Field(default=6, ge=1, le=32)
    retrieval_count: int = Field(default=2, ge=0, le=8)
    history_windows: int = Field(default=3, ge=1, le=24)
    output_retries: int = Field(default=1, ge=0, le=2)
    max_review_interval: int = Field(default=0,ge=0,le=4,strict=True)


class RunConfig11(RunPlan):
    format_version: Literal["1.1"] = "1.1"
    model: ModelConfig

    @property
    def catalog(self):
        return {"shared": self.model}

    def model_id(self, role, depth=None):
        return "shared"


def load_config(data):
    return (RunConfig11 if data.get("format_version") == "1.1" else RunConfig).model_validate(data)


class Message(Record):
    recipients: list[str] = Field(min_length=1)
    text: str = Field(min_length=1)


class Expression(Record):
    text: str = Field(min_length=1)
    messages: list[Message] = Field(default_factory=list)


class MatterUpdate(Record):
    key: Identifier
    text: str = Field(default="", max_length=800)
    status: Literal["open", "closed"] = "open"
    due_window: int | None = Field(default=None,ge=1,strict=True)

    @model_validator(mode="after")
    def has_open_content(self):
        if self.status == "open" and not self.text.strip():
            raise ValueError("新建或更新未完成事项时需要填写内容")
        return self


class AgentMessage(Message):
    enclosures: list[Identifier] = Field(default_factory=list,max_length=4)
    reply_to: str | None = Field(default=None,pattern=r"^e[0-9]{6,12}$")

    @model_validator(mode="after")
    def separate_enclosures(self):
        if len(set(self.enclosures))!=len(self.enclosures):
            raise ValueError("同一封信中无需重复附寄一份原件")
        return self


class Decision(Expression):
    messages: list[AgentMessage] = Field(default_factory=list)
    review_at: int | None = Field(default=None,ge=1,strict=True)
    matters: list[MatterUpdate] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def unique_updates(self):
        if len({m.key for m in self.matters}) != len(self.matters):
            raise ValueError("同一表达中持续事项的编号重复")
        return self


class InfluenceReference(Record):
    source: str
    target: str
    action_ids: list[str] = Field(min_length=1)
    explanation: str = Field(min_length=1)


class FieldReview(Record):
    entity_id: str
    field_id: str
    before: Value
    after: Value
    reason: str = Field(min_length=1)
    action_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)
    influence_refs: list[InfluenceReference] = Field(default_factory=list)


class Consequence(Record):
    text: str
    action_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)


class Settlement(Record):
    reviews: list[FieldReview]
    consequences: list[Consequence] = Field(default_factory=list)
    suggested_fields: list[str] = Field(default_factory=list)
