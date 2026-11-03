"""Honor an actor's own short review schedule using only its private observation."""
from dataclasses import dataclass
import hashlib
import json


def situation(observation):
    # An inbox temporarily adds senders and their containers to the contact list.
    # Its disappearance next round does not change the underlying situation.
    stable={k:observation[k] for k in ("self","background","rules","visible_state","visible_materials","influences")}
    return hashlib.sha256(json.dumps(stable,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).digest()


def revised_deadlines(prior, updates):
    result=dict(prior)
    for matter in updates:
        if matter.status=="closed" or ("due_window" in matter.model_fields_set and matter.due_window is None):
            result.pop(matter.key,None)
        elif matter.due_window is not None:
            result[matter.key]=matter.due_window
    return result


@dataclass(frozen=True)
class ReviewPlan:
    record: str
    review_at: int
    fingerprint: bytes

    @staticmethod
    def next_window(decision, window, limit, deadlines=()):
        if limit and decision.review_at is not None and decision.review_at>window:
            proposed=min(decision.review_at,window+limit)
            revisit=min(deadlines,default=proposed)
            if revisit>window:
                return min(proposed,revisit)
        return None

    @classmethod
    def from_decision(cls, decision, record, observation, limit, deadlines=()):
        next_window=cls.next_window(decision,observation["window"],limit,deadlines)
        return cls(record,next_window,situation(observation)) if next_window else None

    def wake_reason(self, observation):
        if observation.get("messages"):
            return "message"
        if situation(observation)!=self.fingerprint:
            return "visible_change"
        if observation["window"]>=self.review_at:
            return "due"
        return None
