"""Local-clock hints and conservative checks of explicit present-time claims."""
from __future__ import annotations
import re
from datetime import datetime

PERIOD_HOURS = {"凌晨": (0, 6), "半夜": (0, 6), "深夜": (22, 5), "清晨": (4, 9), "早上": (5, 12), "上午": (5, 12), "中午": (11, 14), "下午": (12, 18), "傍晚": (16, 20), "晚上": (17, 24)}
GREETINGS = {"早安": (5, 12), "早上好": (5, 12), "上午好": (5, 12), "午安": (11, 14), "中午好": (11, 14), "下午好": (12, 18), "晚上好": (17, 24), "晚安": (20, 6)}
CURRENT = r"(?:现在|此刻|此时|当前|这会儿|这会)"
PERIOD = "|".join(PERIOD_HOURS)
NUMBER = r"(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})"


def time_brief(clock):
    now = datetime.fromisoformat(clock["now"])
    text = f"本次读取的真实当地时间：{now:%Y年%m月%d日 %H:%M:%S}，{clock['weekday']}，{clock['period']}。时区：{clock['timezone']}（UTC{now:%z}）。"
    if clock.get("last_inbound_at"):
        elapsed = int(clock["elapsed_seconds"])
        text += f"\n用户最后发言：{clock['last_inbound_at']}；距今 {elapsed // 3600} 小时 {(elapsed % 3600) // 60} 分钟，相隔 {clock['day_offset']} 个当地日期。"
    else:
        text += "\n用户最后发言时间未知，不推断用户当前在做什么。"
    return text + "\n以上时钟来自插件，不从聊天历史推断；旧聊天里的现在、今晚、明天不代表此刻。"


def _contains_hour(hour, bounds):
    left, right = bounds
    return left <= hour < right if left < right else hour >= left or hour < right


def _number(value):
    if value.isdigit():
        return int(value)
    digits = {char: index for index, char in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    if "十" in value:
        left, right = value.split("十", 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return digits.get(value, -1)


def _recalled_or_hypothetical(clause, index):
    before = clause[:index]
    return bool(re.search(r"如果|假如|假设|(?:昨[天晚夜]|以前|之前|上次|当时|曾经).{0,20}(?:说|以为|认为|提到|写)|你(?:以为|觉得|说)", before))


def temporal_conflicts(reply, clock, *, morning=False):
    """Check clear current claims only; this is not general semantic inference.

    Preserve questions, quotations, negations and explicit past/future mentions.
    A custom administrator morning window may use an early greeting at any hour;
    it still cannot claim an incorrect current time, date, weekday or daypart.
    """
    now = datetime.fromisoformat(clock["now"])
    clean = re.sub(r"```[\s\S]*?```|`[^`]*`|“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"]*\"|'[^']*'", "", str(reply or ""))
    clean = re.sub(r"(?m)^\s*>.*$", "", clean)
    clean = re.sub(r"[^，,。！？!?；;\n]*[？?]", "", clean)
    issues = []
    for clause in re.split(r"[，,。！？!?；;\n]+", clean):
        if re.search(r"(?:吗|么|呢)\s*$", clause):
            continue
        for match in re.finditer("|".join(GREETINGS), clause):
            word, before, after = match[0], clause[:match.start()], clause[match.end():]
            # A greeting embedded in a longer word is not an explicit greeting:
            # e.g. 下午好好休息 / 今晚安心 / 昨晚安稳. Prefer a missed ambiguous
            # phrase to charging for a correction of an otherwise valid reply.
            greeting_end = r"(?:$|\W|[啊呀啦哦喔哟呦哈呢吧])"
            if not re.match(greeting_end, after) and not re.match(r"(?:主人|朋友们?|大家|各位|宝贝|亲爱的|小可爱|你们?)" + greeting_end, after):
                continue
            if _recalled_or_hypothetical(clause, match.start()):
                continue
            if re.search(r"昨[天晚夜]|前[天晚夜]|上次|那天|当时|过去|明[天晚早]|今晚|以后|到时候|一会|稍后|待会|等会|回头|到时|等.{0,12}(?:再|说)|(?:早上|晚上|下午|中午).{0,8}(?:再|会|说|讲)$|(?:不|别|没).{0,8}(?:说|道|讲|是|算|要|用|想)$", before):
                continue
            if re.search(r"吗|么|这[两几]个字|这个词|这句话|的意思|怎么说|是什么意思", after):
                continue
            if morning and word in {"早安", "早上好", "上午好"}:
                continue
            if not _contains_hour(now.hour, GREETINGS[word]):
                issues.append(f"当前 {now:%H:%M}，直接问候“{word}”与当地时间不符")
        for match in re.finditer(CURRENT + r"(?:已经|正好|正|都|又|才)?(?:是|到|到了)?\s*(" + PERIOD + ")", clause):
            if _recalled_or_hypothetical(clause, match.start()):
                continue
            if not _contains_hour(now.hour, PERIOD_HOURS[match[1]]):
                issues.append(f"当前 {now:%H:%M}，不能将此刻称为{match[1]}")
        for match in re.finditer(CURRENT + r"(?:已经|大约|约|才|都)?(?:是)?\s*(?P<period>" + PERIOD + r")?\s*(?P<hour>" + NUMBER + r")(?:[:：](?P<minute>\d{2})|点(?P<part>半|一刻|三刻|\d{1,2}分)?|时(?P<min2>\d{1,2}分)?)", clause):
            if _recalled_or_hypothetical(clause, match.start()):
                continue
            hour = _number(match["hour"])
            minute = match["minute"] or match["min2"] or match["part"]
            minute = {"半": 30, "一刻": 15, "三刻": 45}.get(minute, int(minute.rstrip("分")) if minute and minute.rstrip("分").isdigit() else None)
            if not 0 <= hour <= 23 or (minute is not None and not 0 <= minute <= 59):
                issues.append("回复中的当前钟点无效")
                continue
            # Twelve-hour expressions include both midnight and noon. Resolve
            # their ambiguity using the named daypart, including intervals
            # crossing midnight (深夜十一点 = 23:00, 深夜一点 = 01:00).
            candidates = [hour % 12, hour % 12 + 12] if hour <= 12 else [hour]
            if match["period"]:
                candidates = [value for value in candidates if _contains_hour(value, PERIOD_HOURS[match["period"]])]
            if minute is None:
                correct = now.hour in candidates
            else:
                current = now.hour * 60 + now.minute
                correct = any(min(abs(current - (h * 60 + minute)), 1440 - abs(current - (h * 60 + minute))) <= 5 for h in candidates)
            if not correct:
                issues.append(f"回复给出的当前钟点与真实 {now:%H:%M} 不符")
        for match in re.finditer(r"今天(?:已经|正好|才)?(?:是)?\s*(?:(?P<year>\d{4})[年/-])?(?P<month>\d{1,2})[月/-](?P<day>\d{1,2})(?:日|号)?", clause):
            if _recalled_or_hypothetical(clause, match.start()):
                continue
            if (int(match["year"] or now.year), int(match["month"]), int(match["day"])) != (now.year, now.month, now.day):
                issues.append(f"回复给出的今天日期与真实 {now:%Y-%m-%d} 不符")
        for match in re.finditer(r"今天(?:已经|正好|才)?(?:是)?\s*(?:星期|周|礼拜)([一二三四五六日天])", clause):
            if _recalled_or_hypothetical(clause, match.start()):
                continue
            if "一二三四五六日".find(match[1].replace("天", "日")) != now.weekday():
                issues.append(f"今天是{clock['weekday']}，回复中的星期不符")
    return list(dict.fromkeys(issues))[:5]
