"""Real-regression harness (real_regression.py): questions and the reading table."""
from __future__ import annotations

import real_regression


def test_loads_the_regression_pack():
    questions = real_regression.load_questions()
    assert len(questions) == 37
    assert {q.question_type for q in questions} == set(real_regression.TYPES)


def test_reading_table_counts_per_type():
    questions = real_regression.load_questions()
    records = [
        {"question": {"question_type": "binary"}, "reading": ["direct"], "final_forecast": 0.4},
        {"question": {"question_type": "binary"}, "reading": ["parser"], "final_forecast": 0.6},
        {"question": {"question_type": "numeric"}, "reading": ["dropped"]},
    ]
    table = real_regression.reading_table(records, questions)
    assert "| binary | 6 | 1 | 1 | 0 | 4 |" in table
    assert "| numeric | 15 | 0 | 0 | 1 | 15 |" in table
    assert "| all | 37 | 1 | 1 | 1 | 35 |" in table
