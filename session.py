import config

def allow_trade(hour, minute):
    current = hour * 60 + minute
    start = 7 * 60        # 07:00
    end = 10 * 60         # 10:00
    return start <= current <= end


def is_session_active(hour, session_name):
    session = config.config["session"][session_name]
    return session["start"] <= hour < session["end"]