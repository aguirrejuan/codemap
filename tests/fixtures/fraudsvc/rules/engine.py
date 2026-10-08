from features import store


class Rule:
    def __init__(self, name, threshold):
        self.name = name
        self.threshold = threshold

    def match(self, feats):
        return self._over(feats)

    def _over(self, feats):
        return feats.get(self.name, 0) > self.threshold


class VelocityRule(Rule):
    pass


def evaluate(txn, feats=None):
    feats = feats or store.get_features(txn["user_id"])
    rule = VelocityRule("velocity", 5)
    return rule.match(feats)
