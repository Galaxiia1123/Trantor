"""A playable two-station handover. The receiving desk acts on the actual enclosure."""
from .case_runs import sample_pair
from .experiments import ExperimentRequest
from .schema import RunConfig11, Scenario11


MODEL="mountain-post-v1"
MANIFEST="一号货袋已封存，批次 K17，清点数为四件。"
HOLD="封存单正在补签，本次只能先通报进度，原件暂时不能外发。"
FILED={2:"按期登记",3:"逾期登记"}


def opening():
    return Scenario11.model_validate({"title":"山线货运站的封存单","background":"两座货站交接一号货袋，第二个班次末截止。",
        "domains":["货物交接"],"entities":[
          {"id":"depot","name":"山顶发货站","identity":"初始持有封存单","preferences":"尽早送单，按授权发件。",
           "materials":[{"domain":"货物交接","text":MANIFEST,"document":{"id":"manifest","title":"一号袋封存单"},
                         "sharing":{"allowed_recipients":["office"]}}]},
          {"id":"office","name":"山下收货站","identity":"实际收件后决定是否登记","preferences":"核对真实副本并按截止时点办理。",
           "fields":[{"id":"entry","label":"交接登记","kind":"string","domain":"货物交接","value":"待单","visibility":{"public":True},
                      "constraints":{"choices":["待单","按期登记","逾期登记"],
                                     "entry_windows":[{"value":"按期登记","last":2},{"value":"逾期登记","first":3}]}}]}],
        "influences":[{"source":"depot","target":"office","description":"发货站可寄送单据，收货站独立核对并办理登记。"}],
        "mail_routes":[{"source":"depot","target":"office","transit_windows":2}],
        "rules":[{"text":"以收货站实际取得的封存单副本核对批次，第二窗末截止，迟到的单据仍可登记。","visibility":{"public":True}}],
        "assumptions":["虚构货站使用本机案例脚本。"]})


def respond(data):
    window=data.get("window")
    if data["task"]=="agent":
        view=data["observation"]
        if data["actor_id"]=="depot" and window==1:
            hold=any(r["text"]==HOLD for r in view["rules"])
            message={"recipients":["office"],"text":"一号货袋已经封存，请按实际到件办理。"}
            if not hold and any(d["id"]=="manifest" for d in view.get("documents",())):
                message["enclosures"]=["manifest"]
            return {"text":"先通报进度，保留原件。" if hold else "寄出封存单副本。","messages":[message]}
        if data["actor_id"]=="office":
            if any(d["id"]=="manifest" for m in view["messages"] for d in m.get("enclosures",())):
                return {"text":FILED[window]+"。我已核对实际收到的封存单副本。","messages":[{"recipients":["depot"],"text":"已收到副本并完成核对。"}]}
            return {"text":"检查本班收件箱，已有登记继续有效；若仍待单，则等实际副本。"}
        return {"text":"等待收货站确认。"}
    if data["task"]=="world":
        before=data["state"]["office"]["entry"]
        arrival=next((m for m in data.get("mail",()) if m["status"]=="arrived" and m.get("enclosures")),None)
        action=next((a for a in data["actions"] if a["actor_id"]=="office" and a["text"].startswith(("按期登记","逾期登记"))),None)
        changed=bool(arrival and action)
        after=FILED[window] if changed else before
        reason="封存单副本实际到达，收货站核对后作出"+after+"决定。" if changed else "本班没有新的核对登记决定。"
        refs={"action_ids":[action["id"]] if changed else [],"event_ids":[arrival["record"]] if changed else []}
        return {"reviews":[{"entity_id":"office","field_id":"entry","before":before,"after":after,"reason":reason,**refs}],
                "consequences":[{"text":reason,**refs}]}
    return None


def run_example(root,choice,gateway):
    config=RunConfig11.model_validate({"model":{"provider":"demo","model":MODEL},"windows":3,"rounds_per_window":1,"window_duration":"一个货运班次"})
    spec=ExperimentRequest(window=0,through=3,kind="repeat")
    if choice=="express":
        spec=ExperimentRequest(window=0,through=3,kind="transit",route={"source":"depot","target":"office","transit_windows":1})
    elif choice=="notice":
        spec=ExperimentRequest(window=0,through=3,kind="condition",condition=HOLD,audience="readers",readers=["depot"])
    return sample_pair(root,gateway,opening(),config,spec,choice,2 if choice=="express" else 3)
