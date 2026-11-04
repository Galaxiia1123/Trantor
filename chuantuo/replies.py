"""A reply names the particular delivered letter the respondent had already received."""


def envelope_key(message):
    return (message["action_id"],message["from"],message["to"],message["text"],message.get("dispatch_id"),
            tuple(d["id"] for d in message.get("enclosures",())),message.get("reply_to"),message.get("reply_by"))


def validate_requested_time(reply_by, window):
    if type(reply_by) is int and reply_by < window:
        raise ValueError(f"回信希望送达的窗口不能早于当前第 {window} 窗。不指定期限时可用 open")


def validate_reply(receipt,actor,recipients,observation_id):
    if (not receipt or receipt["kind"]!="message_delivered" or receipt["data"]["to"]!=actor
            or int(receipt["id"][1:])>=int(observation_id[1:])):
        raise ValueError("reply_to 只能填写你在作此决定前实际收到的信件 receipt_id")
    if set(recipients)!={receipt["data"]["from"]}:
        raise ValueError("带 reply_to 的信须直接回复该封来信的发件人。另发给其他主体时使用独立消息")
