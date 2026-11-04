"""A route experiment can reschedule one sealed envelope while it is still in transit."""
import hashlib
import json


def seal(postmark):
    payload=json.dumps(postmark["data"],sort_keys=True,ensure_ascii=False,separators=(",",":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class MailTiming:
    def __init__(self, marker=None):
        self.by_dispatch={}
        for edit in (marker or {}).get("mail_timing",()):
            if type(edit.get("after")) is not int or not 1<=edit["after"]<=marker.get("window",0):
                raise ValueError("改期必须源自已提交的在途时点")
            self.by_dispatch.setdefault(edit["dispatch"],[]).append(edit)

    def due(self, postmark, after_window):
        due=postmark["data"]["due_window"]
        for edit in self.by_dispatch.get(postmark["id"],()):
            if edit["after"]<=after_window:
                due=edit["new"]
        return due

    def check_dispatch(self, postmark):
        edits=self.by_dispatch.get(postmark["id"])
        if not edits:return
        expected=postmark["data"]["due_window"]
        previous=postmark["window"]
        digest=seal(postmark)
        for edit in edits:
            if (edit["seal"]!=digest or type(edit["after"]) is not int or type(edit["new"]) is not int
                    or not previous<=edit["after"]<expected or edit["old"]!=expected or edit["new"]<=edit["after"]
                    or edit["new"]==expected):
                raise ValueError("单封信的改期依据与原始发件、在途时段或原预计日期不符")
            expected=edit["new"]
            previous=edit["after"]

    def receipt(self, postmark, window):
        data={**postmark["data"],"dispatch_id":postmark["id"]}
        if window!=data["due_window"]:
            data["arrived_window"]=window
        return data
