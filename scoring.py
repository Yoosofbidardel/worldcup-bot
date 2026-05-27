from config import PTS_EXACT, PTS_CORRECT_DIFF, PTS_CORRECT_OUTCOME


def calculate_points(pred_home: int, pred_away: int, actual_home: int, actual_away: int) -> int:
    """
    Scoring rules:
      10 pts – exact score
       7 pts – correct winner + correct goal difference (includes draw with wrong scoreline)
       5 pts – correct winner, wrong goal difference
       0 pts – wrong outcome
    """
    if pred_home == actual_home and pred_away == actual_away:
        return PTS_EXACT

    pred_diff = pred_home - pred_away
    actual_diff = actual_home - actual_away

    # Determine outcome: positive = home win, negative = away win, zero = draw
    def outcome(diff):
        if diff > 0:
            return "home"
        if diff < 0:
            return "away"
        return "draw"

    if outcome(pred_diff) != outcome(actual_diff):
        return 0

    # Correct outcome — check goal difference
    if pred_diff == actual_diff:
        return PTS_CORRECT_DIFF

    return PTS_CORRECT_OUTCOME
