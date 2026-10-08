def velocity(raw):
    return len(raw)


def device_score(raw):
    return sum(1 for r in raw if r.get("new_device"))


def compute_all(rows):
    return [(velocity(r), device_score(r)) for r in rows]
