"""The day in short plain-text lines, and the wording shared with it."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# Return-home events shortly after midnight belong to the prior evening as
# well as the morning on which they happened. This is deliberately a factual
# window, not an inference about whether someone stayed somewhere overnight.
HOME_RETURN_GRACE_HOURS = 6


def home_return_context(day_start, day_end):
    """The neighboring samples that can establish an early-morning Home return."""
    return (day_start - timedelta(days=1),
            day_end + timedelta(hours=HOME_RETURN_GRACE_HOURS) - timedelta(microseconds=1))


def home_return_text(home_return, zone):
    returned = datetime.fromisoformat(home_return["returned_at"]).astimezone(zone).strftime("%H:%M")
    target = "home" if str(home_return["home_name"]).casefold() in ("home", "my home") else home_return["home_name"]
    action = f"Returned {target}" if target == "home" else f"Returned to {target}"
    suffix = "the next day" if home_return["timing"] == "next_day" else "after midnight"
    return f"{action} at {returned} {suffix}."


BRIEF_MOVES = {"walking": "walked {}", "running": "ran {}", "cycling": "cycled {}",
               "driving": "drove {}", "transit": "rode {} by transit"}


def _brief_duration(minutes):
    hours, rest = divmod(round(minutes or 0), 60)
    if not hours:
        return f"{rest} min"
    return f"{hours} h {rest} min" if rest else f"{hours} h"


_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "INR": "₹"}


def money_text(amount, currency):
    """$4.20 for a symbol or a well-known code, 4.20 NPR for any other code."""
    currency = currency or "USD"
    symbol = _CURRENCY_SYMBOLS.get(currency, currency)
    return f"{amount:.2f} {currency}" if re.fullmatch(r"[A-Za-z]{2,}", symbol) else f"{symbol}{amount:.2f}"


def _brief_move(mode, distance_km):
    return BRIEF_MOVES.get(mode, mode + " {}").format(f"{distance_km or 0:.1f} km")


def _brief_count(count, noun):
    return f"{count} {noun}{'' if count == 1 else 's'}"


def daily_brief(overview):
    """The day in short plain-text lines, for an LLM to read cheaply.

    Derived from the full overview, so the two never disagree. Only what
    happened is stated: no false flags, empty lists, ids, or coordinates.
    Times are local HH:MM; a time on another day carries its date.
    """
    zone = ZoneInfo(overview["timezone"])
    day = datetime.strptime(overview["date"], "%Y-%m-%d").date()

    def local(value):
        return datetime.fromisoformat(value).astimezone(zone)

    def clock(moment):
        return moment.strftime("%H:%M") if moment.date() == day else moment.strftime("%Y-%m-%d %H:%M")

    def reminder(item, at):
        place = f"any place named {item['match_name']}" if item.get("match_name") else item.get("place_name")
        return f"{clock(local(item[at]))} '{item['text']}'" + (f" at {place}" if place else "")

    brief = {"date": f"{day.strftime('%A')}, {day.isoformat()} ({overview['timezone']})"}
    if overview.get("source"):
        brief["source"] = overview["source"]
    totals = overview["totals"]
    if not totals["raw_fixes"]:
        brief["summary"] = "No location recorded."
    else:
        trips = overview["trips"]
        summary = f"{_brief_count(len(overview['places']), 'place')}, {_brief_count(len(trips), 'trip')}"
        summary += f" ({totals['distance_km']:.1f} km)." if trips else "."
        moves = [f"{_brief_move(mode, values['distance_km'])} ({_brief_duration(values['minutes'])})"
                 for mode, values in overview["activity_totals"].items() if values["count"]]
        if moves:
            joined = ", ".join(moves)
            summary += " " + joined[0].upper() + joined[1:] + "."
        if totals.get("fuel_cost") is not None:
            summary += f" Fuel about {money_text(totals['fuel_cost'], totals.get('currency'))}."
        brief["summary"] = summary
    timeline = []
    for visit in overview["visits"]:
        arrival, departure = local(visit["arrival"]), local(visit["departure"])
        before, after = arrival.date() < day, departure.date() > day
        if before and (after or visit["ongoing"]):
            span = "all day"
        elif before:
            span = f"until {clock(departure)}"
        elif after or visit["ongoing"]:
            span = f"from {clock(arrival)}"
        else:
            span = f"{clock(arrival)}-{clock(departure)}"
        text = f"{span} at {visit['place'] or 'an unnamed place'}"
        if visit.get("zone") and visit["zone"] != visit["place"]:
            text += f" ({visit['zone']})"
        if visit.get("activity"):
            text += ", " + (_brief_move(visit["activity"], visit["distance_km"]) if visit.get("distance_km")
                            else visit["activity"])
        if visit.get("moved_within"):
            text += "; within it " + ", ".join(_brief_move(move["mode"], move["distance_km"]) for move in visit["moved_within"])
        if visit["ongoing"] and not after:
            # The latest stay: "still there" as of the last fix, not necessarily now.
            text += f", still there at {clock(departure)}"
        timeline.append((arrival, text))
    for trip in overview["trips"]:
        start = local(trip["start"])
        text = f"{clock(start)}-{clock(local(trip['end']))} {_brief_move(trip['mode'], trip['distance_km'])}"
        if trip["round_trip"]:
            text += f" round trip from {trip['from'] or 'an unnamed place'}" + (f" via {trip['via']}" if trip.get("via") else "")
        else:
            text += (f" from {trip['from']}" if trip["from"] else "") + (f" to {trip['to']}" if trip["to"] else "")
        if not trip["coverage_complete"]:
            text += ", partly unrecorded"
        timeline.append((start, text))
    for gap in overview["coverage_gaps"]:
        start = local(gap["start"])
        timeline.append((start, f"{clock(start)}-{clock(local(gap['end']))} no location data"))
    if timeline:
        brief["timeline"] = [text for _moment, text in sorted(timeline, key=lambda item: item[0])]
    places = []
    for place in overview["places"]:
        name = place["name"] or place["address"] or "Unnamed place"
        category = (place.get("category") or "").split(",")[0].rsplit(".", 1)[-1].replace("_", " ")
        if category.lower() in name.lower():
            category = ""
        zone_name = place.get("zone") if place.get("zone") != name else ""
        if category and category.lower() in (zone_name or "").lower():
            category = ""
        details = [item for item in (zone_name, category, place["address"] if place["address"] != name else "") if item]
        text = name + (f" ({', '.join(details)})" if details else "") + ": " + _brief_duration(place["observed_minutes"])
        if place["visits"] > 1:
            text += f" over {place['visits']} visits"
        activities = [mode if count == 1 else f"{mode} x{count}" for mode, count in (place.get("activities") or {}).items()]
        if activities:
            text += ", " + ", ".join(activities)
        if place["home"] and name.lower() != "home":
            text += ", home"
        places.append(text)
    if places:
        brief["places"] = places
    if overview.get("zones"):
        brief["zones"] = [f"{item['name']}: {_brief_duration(item['observed_minutes'])} at {', '.join(item['places'])}"
                          for item in overview["zones"]]
    if overview["reminders_created"]:
        brief["reminders_set"] = [reminder(item, "created_at") for item in overview["reminders_created"]]
    if overview["reminders_triggered"]:
        brief["reminders_triggered"] = [reminder(item, "triggered_at") for item in overview["reminders_triggered"]]
    if overview.get("home_return"):
        brief["home_return"] = overview["home_return"]["summary"]
    return brief
