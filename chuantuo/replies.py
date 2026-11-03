"""A reply names the particular delivered letter the respondent had already received."""


def envelope_key(message):
    return (message["action_id"],message["from"],message["to"],message["text"],message.get("dispatch_id"),
            tuple(d["id"] for d in message.get("enclosures",())),message.get("reply_to"))


def validate_reply(receipt,actor,recipients,observation_id):
    if (not receipt or receipt["kind"]!="message_delivered" or receipt["data"]["to"]!=actor
            or int(receipt["id"][1:])>=int(observation_id[1:])):
        raise ValueError("reply_to 只能填写你在作此决定前实际收到的信件 receipt_id")
    if set(recipients)!={receipt["data"]["from"]}:
        raise ValueError("带 reply_to 的信须直接回复该封来信的发件人。另发给其他主体时使用独立消息")
