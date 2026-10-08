from features import get_features
from rules.engine import evaluate


def score_endpoint(txn):
    feats = get_features(txn["user_id"])
    return evaluate(txn, feats)
