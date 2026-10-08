from .compute import device_score, velocity


class FeatureStore:
    def __init__(self):
        self.cache = {}

    def load(self, user_id):
        return self.cache.get(user_id, [])


def get_features(user_id):
    fs = FeatureStore()
    raw = fs.load(user_id)
    return {"velocity": velocity(raw), "device": device_score(raw)}
