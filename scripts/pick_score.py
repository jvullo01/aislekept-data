"""Approximate AisleKept health score used to rank the top picks of each category.

It mirrors the app's HealthScoreCalculator (nutrition 60, additives 25, processing 15) closely
enough to choose ten candidates per category. The app re-scores every candidate with its own
calculator before showing a better pick, so small differences here never reach the shopper.
"""

import heapq

TOP_PICKS_PER_CATEGORY = 10

# (row position, medium above, high above, medium penalty, high penalty), as in the app.
LIMITED_NUTRIENTS = (
    (6, 5.0, 22.5, 8, 18),     # sugars
    (5, 1.5, 5.0, 6, 14),      # saturated fat
    (7, 0.3, 1.5, 6, 14),      # salt
    (4, 3.0, 17.5, 2, 6),      # fat
    (3, 250.0, 400.0, 4, 8),   # calories
)
# (row position, source at least, rich at least, source bonus, rich bonus)
BENEFICIAL_NUTRIENTS = (
    (8, 3.0, 6.0, 3, 6),       # fiber
    (9, 5.0, 10.0, 2, 4),      # protein
)
PROCESSING_POINTS = {1: 15, 2: 12, 3: 8, 4: 0}
UNKNOWN_PROCESSING_POINTS = 8
ASSUMED_ADDITIVE_PENALTY = 4  # the app's "limit" tier; its risk table is not shipped with the data


def nutrient_points(row):
    change = 0
    for position, medium_above, high_above, medium_penalty, high_penalty in LIMITED_NUTRIENTS:
        amount = row[position]
        if amount is None:
            change -= high_penalty // 2
        elif amount > high_above:
            change -= high_penalty
        elif amount > medium_above:
            change -= medium_penalty
    for position, source_at_least, rich_at_least, source_bonus, rich_bonus in BENEFICIAL_NUTRIENTS:
        amount = row[position]
        if amount is not None and amount >= rich_at_least:
            change += rich_bonus
        elif amount is not None and amount >= source_at_least:
            change += source_bonus
    return min(60, max(0, 60 + change))


def additive_points(additive_codes):
    count = len([code for code in (additive_codes or "").split(",") if code.strip()])
    return max(0, 25 - ASSUMED_ADDITIVE_PENALTY * count)


def approximate_score(row):
    """0-100 score for a selected row (column order of build_product_database.selection_query)."""
    processing = PROCESSING_POINTS.get(row[12], UNKNOWN_PROCESSING_POINTS)
    return nutrient_points(row) + additive_points(row[10]) + processing


class TopPicks:
    """Keeps the best `limit` barcodes of each category while rows stream past."""

    def __init__(self, limit):
        self.limit = limit
        self.heaps = {}

    def offer(self, category_id, barcode, score):
        heap = self.heaps.setdefault(category_id, [])
        entry = (score, -barcode)  # ties go to the lower barcode, so builds are repeatable
        if len(heap) < self.limit:
            heapq.heappush(heap, entry)
        elif entry > heap[0]:
            heapq.heapreplace(heap, entry)

    def rows(self):
        """(category_id, rank, barcode, score) with rank 1 the best."""
        result = []
        for category_id, heap in self.heaps.items():
            ranked = sorted(heap, reverse=True)
            result.extend((category_id, rank, -negative_barcode, score)
                          for rank, (score, negative_barcode) in enumerate(ranked, start=1))
        return result
