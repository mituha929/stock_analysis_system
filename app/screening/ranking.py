def rank_results(rows, sort):
    """Stable lexicographic sorting, missing values last in either direction."""
    result = sorted(rows, key=lambda row: (row["symbol"], row["market_no"]))
    for spec in reversed(sort):
        present = [row for row in result if row.get(spec.field) is not None]
        missing = [row for row in result if row.get(spec.field) is None]
        result = sorted(present, key=lambda row: row[spec.field], reverse=spec.direction == "desc") + missing
    return [{**row, "rank": index + 1} for index, row in enumerate(result)]
