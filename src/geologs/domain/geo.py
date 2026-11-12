"""Distances on the Earth's surface."""
import math


def great_circle(a, b, steps=64):
    """[lon, lat] points along the shortest way over the globe from a to b ([lon, lat]).

    Longitudes run on past ±180 rather than wrap, so a line across the
    antimeridian is drawn the short way round.
    """
    (lon1, lat1), (lon2, lat2) = a, b
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    ends = [(math.cos(p) * math.cos(l), math.cos(p) * math.sin(l), math.sin(p)) for p, l in ((p1, l1), (p2, l2))]
    angle = math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(*ends)))))
    if angle < 1e-9:
        return [list(a), list(b)]
    points = []
    for index in range(steps + 1):
        f = index / steps
        wa, wb = math.sin((1 - f) * angle) / math.sin(angle), math.sin(f * angle) / math.sin(angle)
        x, y, z = (wa * u + wb * v for u, v in zip(*ends))
        lon = math.degrees(math.atan2(y, x))
        if points:
            lon += 360 * round((points[-1][0] - lon) / 360)
        points.append([lon, math.degrees(math.atan2(z, math.hypot(x, y)))])
    points[0], points[-1][1] = list(a), lat2
    return points


def metres(a_lat, a_lon, b_lat, b_lon):
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = p2 - p1
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 12742000 * math.asin(min(1, math.sqrt(h)))
